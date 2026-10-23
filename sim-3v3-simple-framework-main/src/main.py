"""SoccerSim 策略入口 —— 比赛策略主逻辑都在这里,改打法就改这个文件。

结构(由浅入深):
- main.py(本文件):比赛策略。play() 按 Phase 状态机分派到 _act_*;各 _act_* 选出
  角色(门将/攻击/支援)并直接调 player 动作。
- player.py:Player 控制 handle + 高层动作(push_ball / attack / guard / support /
  defend / stage_behind_and_kick / retreat_to / walk_to)。
- utils/:走位/几何/避障工具(opponent_goal / dist / angle_to ...)。
- framework/:平台管线,用户不改。

战术骨架(1-1-1 动态三角):
    门将(GK)   门线前滑动封角,危险时出击;对方定位球时贴门线拿 R-07 豁免
    攻击(ATK)  四级决策树:解围 > 射门 > 传球 > 五模式推球
    支援(SUP)  我方控球→前插接应;对方控球→回撤补防;按比分/时间改门槛

场景自适应(不改顶层流程,只在 support()/attack() 内部读 match_mode()):
    park   领先/死守   → 过中线就解围,Support 退本方补防位
    allin  终场落后   → 放宽射门距离,Support 压到对方禁区边缘
    press  落后       → 前插门槛降低,更敢压上
    hold / steady     → 前插门槛提高,收缩不冒险

合规红线(违反 = 罚下 30s):
    - SET / stopped 阶段必须静止(M-01 / M-02)
    - 对方定位球必须退 ≥1.65m 且不碰球(R-06 / R-07)
    - 开球不能直接射门(G-03 需 ≥2 次触球);角球/球门球/界外球可以直接射
    - 所有站位点必须过 _clamp_field / _clamp_own_half / _clamp_out_circle_keep_y
"""

from __future__ import annotations

import logging
import math
from enum import Enum

from booster_agent_framework import AgentBase

from .framework.agent import SoccerAgentMixin
from .framework.types import KICKING_TEAM_NONE, Context, GameState, SetPlay
from .param import *
from .player import Player, we_have_ball
from .utils import angle_to, clamp, dist, opponent_goal, own_goal


_log = logging.getLogger(__name__)


# ======================================================================
# Phase 状态机 —— 比赛阶段分类
# ======================================================================


class Phase(Enum):
    """比赛阶段。顶层状态机,决定当前是正常拼抢/开球/定位球/准备/停止。"""
    NORMAL = "normal"              # PLAYING 正常拼抢
    OUR_KICKOFF = "our_kickoff"    # 我方开球(SET+PLAYING 初期)
    OPP_KICKOFF = "opp_kickoff"    # 对方开球(避让)
    OUR_SET_PLAY = "our_set_play"  # 我方定位球(任意球/角球/球门球)
    OPP_SET_PLAY = "opp_set_play"  # 对方定位球(避让)
    READY = "ready"                # READY 走位
    STOPPED = "stopped"            # SET(非开球重开) / INITIAL / FINISHED / stopped


def get_phase(context: Context) -> Phase:
    """根据裁判机状态判断当前比赛阶段。"""
    g = context.game
    if g is None:
        return Phase.STOPPED

    state = g.state

    # READY:走 ready 位
    if state == GameState.READY:
        return Phase.READY

    # PLAYING:正常拼抢 or 开球/定位球执行中
    if state == GameState.PLAYING and not g.stopped:
        # 定位球:set_play != NONE,kicking_team 指示哪方
        if g.set_play != SetPlay.NONE and g.kicking_team != KICKING_TEAM_NONE:
            our_team = context.team_id
            if g.kicking_team == our_team:
                return Phase.OUR_SET_PLAY
            else:
                return Phase.OPP_SET_PLAY

        # 开球:secondary_time > 0(倒计时窗口),kicking_team 指示哪方
        if g.secondary_time > 0 and g.kicking_team != KICKING_TEAM_NONE:
            our_team = context.team_id
            if g.kicking_team == our_team:
                return Phase.OUR_KICKOFF
            else:
                return Phase.OPP_KICKOFF

        # 正常拼抢
        return Phase.NORMAL

    # SET / INITIAL / FINISHED / stopped:站定
    return Phase.STOPPED


def get_set_play_type(context: Context) -> SetPlay:
    """当前生效的定位球类型;无定位球(或无裁判机数据)时返回 ``SetPlay.NONE``。

    共 7 种可能返回值(见 framework.types.SetPlay):
    - ``NONE``:无定位球(正常比赛/开球等)
    - ``DIRECT_FREE_KICK``:直接任意球(可直接射门得分)
    - ``INDIRECT_FREE_KICK``:间接任意球(须先触碰他人才能进球)
    - ``PENALTY_KICK``:点球
    - ``THROW_IN``:界外球(踢入)
    - ``GOAL_KICK``:球门球
    - ``CORNER_KICK``:角球
    """
    g = context.game
    if g is None:
        return SetPlay.NONE
    return g.set_play


# ======================================================================
# 公共工具:站位合法化 / 选人 / 定位球落点
# ======================================================================


def _clamp_field(
    context: Context, point: tuple[float, float], margin: float = LEGAL_MARGIN_M,
) -> tuple[float, float]:
    """把站位点夹进场地内(留 margin 余量)。所有站位点的第一道保险。"""
    f = context.field
    half_l = f.length / 2.0 - margin
    half_w = f.width / 2.0 - margin
    return (
        max(-half_l, min(half_l, point[0])),
        max(-half_w, min(half_w, point[1])),
    )


def _clamp_own_half(
    context: Context, point: tuple[float, float], margin: float = LEGAL_MARGIN_M,
) -> tuple[float, float]:
    """夹进**本方半场**(x ≤ -margin)。SET / 开球前站位用(P-01)。"""
    x, y = _clamp_field(context, point, margin)
    return (min(x, -margin), y)


def _clamp_outside_circle(
    context: Context, point: tuple[float, float], margin: float = CIRCLE_MARGIN_M,
) -> tuple[float, float]:
    """把点推到中圈外(离原点 ≥ 中圈半径 + margin)。非开球方站位用(P-01/P-02)。"""
    r = context.field.circle_radius + margin
    x, y = point
    d = math.hypot(x, y)
    if d >= r:
        return (x, y)
    if d < 1e-6:
        return (-r, 0.0)
    return (x / d * r, y / d * r)


def _clamp_out_circle_keep_y(
    context: Context, point: tuple[float, float], margin: float = CIRCLE_MARGIN_M,
) -> tuple[float, float]:
    """把点推到中圈外,但**只动 x**,保留 y。

    ``_clamp_outside_circle`` 是沿径向推的,盯人时会把 y 也带偏,盯的人就不是
    那个人了。对方开球站位要求"中圈外 + 本方半场",沿 x 推既满足合规,
    又能让 y 继续对准要盯的对手。
    """
    r = context.field.circle_radius + margin
    x, y = point
    if math.hypot(x, y) >= r:
        return (x, y)
    if abs(y) >= r:
        return (x, y)                       # y 已经够远,x 不用动
    return (-math.sqrt(max(r * r - y * y, 0.0)), y)


def _mark_point(
    context: Context, opp_pose, mark_dist: float = OPP_KICKOFF_MARK_DIST_M,
) -> tuple[float, float]:
    """站在对手「靠我方球门」一侧 ``mark_dist`` 处 —— 堵他往球门的出球线。"""
    gx, gy = own_goal(context)
    a = angle_to(opp_pose.x, opp_pose.y, gx, gy)
    return (
        opp_pose.x + mark_dist * math.cos(a),
        opp_pose.y + mark_dist * math.sin(a),
    )


def _nearest_player_to(
    players: list[Player], x: float, y: float,
) -> Player | None:
    """离 (x,y) 最近的球员(pose 已知)。"""
    best, best_d = None, math.inf
    for p in players:
        if p.pose is None:
            continue
        d = dist(p.pose.x, p.pose.y, x, y)
        if d < best_d:
            best, best_d = p, d
    return best


def _pick_goalkeeper(
    context: Context, players: list[Player], store,
) -> Player | None:
    """选门将并锁定(避免每帧换人导致两人来回跑)。

    锁定规则:store.gk_id 有值且该球员本帧可行动 → 沿用;
    否则选离己方门最近者,并写回 store。
    """
    if not players:
        return None
    gk = next((p for p in players if p.id == store.gk_id), None)
    if gk is not None:
        store.gk_missing_since = None
        return gk
    # 门将不在 active 里(可能摔倒/起身中):给宽限,别立刻换人
    if store.gk_id is not None:
        if store.gk_missing_since is None:
            store.gk_missing_since = context.now
        elif context.now - store.gk_missing_since < GK_SWITCH_GRACE_S:
            # 宽限期内:从全体 players 里找旧门将,哪怕它不在 active 里
            gk = next(
                (p for p in context.teammates.values()
                 if p.pose is not None and p.id == store.gk_id),
                None,
            )
            if gk is not None:
                return gk
    # 宽限过了或没有旧门将 → 重选
    gx, gy = own_goal(context)
    gk = _nearest_player_to(players, gx, gy)
    if gk is not None:
        store.gk_id = gk.id
    store.gk_missing_since = None
    return gk


def _set_play_aim(
    context: Context, set_play: SetPlay, ball,
    support_point: tuple[float, float] | None = None,
) -> tuple[tuple[float, float], float]:
    """按定位球类型给出"踢向哪、多大劲"。

    - 角球:传中到门前（不瞄接应点，瞄禁区里的落点）
    - 球门球 / 界外球 / 间接任意球:**踢给接应队友**（aim 与 support_point 一致，
      否则主罚者往 A 点传、队友却在 B 点跑，等于把球送给对手）
    - 直接任意球 / 点球:能射就射，否则给队友
    """
    f = context.field
    half_l = f.length / 2.0
    side = 1.0 if ball.y >= 0.0 else -1.0
    # 没有给接应点时退化成"向前推一段"，保证函数自洽
    fallback = _clamp_field(context, (ball.x + 3.0, side * 2.0))
    receive = support_point if support_point is not None else fallback

    if set_play == SetPlay.CORNER_KICK:
        return (
            _clamp_field(context, (half_l - CORNER_AIM_X_BACK_M, side * CORNER_AIM_Y)),
            SETPLAY_PASS_POWER,
        )

    if set_play == SetPlay.GOAL_KICK:
        return (receive, SETPLAY_CLEAR_POWER)

    if set_play == SetPlay.THROW_IN:
        return (receive, SETPLAY_PASS_POWER)

    if set_play in (SetPlay.DIRECT_FREE_KICK, SetPlay.PENALTY_KICK):
        goal = opponent_goal(context)
        if dist(ball.x, ball.y, goal[0], goal[1]) <= SHOOT_RANGE_M:
            return (goal, SHOOT_POWER_MID)
        return (receive, SETPLAY_PASS_POWER)

    # INDIRECT_FREE_KICK / 其它:传给队友,不许直接射门得分
    return (receive, SETPLAY_PASS_POWER)


def _set_play_support_point(
    context: Context, set_play: SetPlay, ball,
) -> tuple[float, float]:
    """定位球时非主罚球员的站位点(也是主罚者的传球目标,两者必须一致)。"""
    f = context.field
    half_l = f.length / 2.0
    side = 1.0 if ball.y >= 0.0 else -1.0

    if set_play == SetPlay.CORNER_KICK:
        # 接应点必须和 _set_play_aim 的落点**一致** —— 主罚者往 A 点传、
        # 队友却跑 B 点,等于把球传给空气。
        return _clamp_field(
            context, (half_l - CORNER_AIM_X_BACK_M, side * CORNER_AIM_Y),
        )
    if set_play == SetPlay.GOAL_KICK:
        # 接应球门球:前场边路(R-03:球门球摆在 x≈±6，所以这里给绝对坐标)
        return _clamp_field(context, (GK_OUT_X, side * GK_OUT_Y))
    if set_play == SetPlay.THROW_IN:
        # 界外球:向前推进一段、往中路拉开
        return _clamp_field(
            context, (ball.x + THROW_IN_SAFE_X, ball.y - side * 2.0),
        )
    # 其余:球前方偏侧,准备接球
    return _clamp_field(context, (ball.x + 2.5, side * 2.0))


def _goal_aperture_deg(context: Context, ball) -> float:
    """从球位看对方球门的张角(度)。

    这是"定位球能不能一脚射进"的唯一硬指标:
      角球摆在角旗区 (6.95, ±4.45),球和球门同在底线上 → 张角 0.41°,
          需要 0.2° 的方向精度,物理上射不进(球会沿底线滚出界);
      球门球 (-6, ±2) → 张角 11°,但距离 13m,必被 3 个防守者挡;
      对方半场边线的界外球 (5, ±4.45) → 张角 13°,距离 4.9m,这才射得进。
    """
    f = context.field
    half_l = f.length / 2.0
    half = f.goal_width / 2.0
    dx = half_l - ball.x
    if dx <= 1e-6:
        return 0.0
    a1 = math.atan2(half - ball.y, dx)
    a2 = math.atan2(-half - ball.y, dx)
    return abs(math.degrees(a1 - a2))


def _setplay_shot(
    context: Context, set_play: SetPlay, ball, taker: Player,
) -> tuple[tuple[float, float], float] | None:
    """定位球能否一脚直接打进。返回 (落点, 力度) 或 None。

    规则上只有 kick-off 需要 ≥2 次触球(G-03);角球/球门球/界外球踢进对方
    球门都有效(G-04 从反面印证:定位球乌龙才不加分)。所以只看几何。
    """
    # 间接任意球直接进球无效,必须先给队友
    if set_play == SetPlay.INDIRECT_FREE_KICK:
        return None

    goal = opponent_goal(context)
    if dist(ball.x, ball.y, goal[0], goal[1]) > SETPLAY_SHOOT_RANGE_M:
        return None
    if _goal_aperture_deg(context, ball) < SETPLAY_SHOOT_MIN_APERTURE_DEG:
        return None

    aim = taker._best_goal_target(context, ball)
    if taker._lane_clearance(
        context, ball.x, ball.y, aim[0], aim[1],
    ) < SETPLAY_SHOOT_CLEAR_M:
        return None
    return aim, taker._shoot_power(context, ball, aim)


# ======================================================================
# Agent 入口
# ======================================================================


class SoccerSimAgent(SoccerAgentMixin, AgentBase):
    """3v3 SoccerSim agent。"""

    player_class = Player

    def init_store(self, store) -> None:
        _log.info("init_store called")
        store.prev_phase = None        # 上一帧 phase,用于检测 phase 跳变(边沿)
        store.cur_phase = None
        store.kickoff_taker = None     # 锁定的开球主罚球员 id(每次进入开球时重选)
        store.normal_attacker = None   # 正常阶段的攻击手(带迟滞,防角色抖动)
        store.gk_id = None             # 锁定的门将 id
        store.gk_missing_since = None  # 门将不在 active 列表里持续多久
        store.prev_ball = None         # 上一帧球位(检测裁判摆球/坠球复位)
        store.ball_vx = 0.0            # 球速(帧差估算,用于防守预判)
        store.ball_vy = 0.0
        store.setplay_ball = None      # 进入我方定位球时的球位(判断球是否已被踢出)
        store.setplay_kicked = False   # 主罚者是否已经踢出(避免裁判摆球误判)
        store.setplay_kicked_at = None # 首次踢出的时刻(踢空兜底用)
        store.setplay_taker = None     # 锁定的定位球主罚者 id

    @staticmethod
    def play(context: Context, players: list[Player], store) -> None:
        phase = get_phase(context)
        store.prev_phase = store.cur_phase
        store.cur_phase = phase

        # 画可视化(每帧)
        _analyze_and_draw(context, players, store)

        # 当前 phase 以 label 画在场外。
        from .framework import debugdraw
        g = context.game
        game_state = g.state.value if g is not None else "none"
        set_play = g.set_play.value if g is not None else "none"
        secondary_time = g.secondary_time if g is not None else 0.0
        stopped = g.stopped if g is not None else False
        debugdraw.text(
            0.0, context.field.width / 2.0 + 0.2,
            f"phase={phase.value} state={game_state} set={set_play} "
            f"secondary={secondary_time:.1f} stopped={stopped}",
            rgb=(1.0, 1.0, 0.0), ns="phase",
        )

        # 球位单帧跳变检测:裁判摆球/坠球复位时球位会突然跳 >2m。
        # 不清空缓存的话，机器人会认为"我已经站在目标点上了"继续僵住。
        ball = context.ball
        if ball is not None:
            if store.prev_ball is not None:
                jump = dist(ball.x, ball.y, store.prev_ball[0], store.prev_ball[1])
                if jump > BALL_JUMP_M:
                    _reset_sticky(store, players)
                # 帧差球速:用于防守预判拦截点(问题 1/6)
                store.ball_vx = (ball.x - store.prev_ball[0]) / context.dt
                store.ball_vy = (ball.y - store.prev_ball[1]) / context.dt
            else:
                store.ball_vx = 0.0
                store.ball_vy = 0.0
            store.prev_ball = (ball.x, ball.y)

        # 活性自理 + 过滤出本帧可行动的球员。
        # ensure_ready:摔倒起身 / 切 walk 模式(异步,不产生移动);被罚下的也做,
        # 这样解罚后能立刻投入。被罚下或未就绪的不参与分派。
        active: list[Player] = []
        for p in players:
            ready = p.ensure_ready()
            if p.is_penalized:
                p.action = "penalized"     # 罚下:可起身/切模式,但不能移动
                p.stop()
            elif not ready:
                # 未就绪必须停车:残留的 set_velocity 会一直生效,机器人会
                # 一边躺/一边切模式一边蹬腿,既起不来也可能再摔一次。
                p.action = "fallen" if p.is_fallen else "switching_mode"
                p.stop()
            elif p.pose is None:
                p.action = "no_pose"       # 自己位置未知:不参与分派
                p.stop()
            else:
                active.append(p)

        # 按 phase 对整队分派一次(角色分配等全队计算只在 _act_* 里算一次)。
        if phase == Phase.NORMAL:
            _act_normal(context, active, store)
        elif phase == Phase.OUR_KICKOFF:
            _clear_normal_sticky(store)
            _act_our_kickoff(context, active, store)
        elif phase == Phase.OPP_KICKOFF:
            _clear_normal_sticky(store)
            _act_opp_kickoff(context, active, store)
        elif phase == Phase.OUR_SET_PLAY:
            _clear_normal_sticky(store)
            # 刚进入我方定位球:重置球位基准(否则会拿上一轮的旧球位误判"已踢出")
            if store.prev_phase != Phase.OUR_SET_PLAY:
                store.setplay_ball = None
                store.setplay_kicked = False
                store.setplay_kicked_at = None
                store.setplay_taker = None
            _act_our_set_play(context, active, store)
        elif phase == Phase.OPP_SET_PLAY:
            _clear_normal_sticky(store)
            _act_opp_set_play(context, active, store)
        elif phase == Phase.READY:
            _clear_normal_sticky(store)
            _act_ready(context, active, store)
        elif phase == Phase.STOPPED:
            _clear_normal_sticky(store)
            store.setplay_ball = None
            for p in active:
                p.action = "stopped"
                p.stop()

        # --- 反静止 M-05 ---
        _anti_idle(context, active, store, phase)

        # 队员可视化统一在最后画一遍:覆盖所有球员(含判罚/未就绪/STOPPED)。
        for p in players:
            _draw_teammate_marker(p)


def _reset_sticky(store, players: list[Player]) -> None:
    """球位被裁判复位时清空所有跨帧缓存,否则机器人会僵住。"""
    store.normal_attacker = None
    store.setplay_ball = None
    store.setplay_kicked = False
    store.setplay_kicked_at = None
    store.setplay_taker = None
    for p in players:
        p._push_mode = None
        p._support_last_pos = None
        p._support_stationary_since = None
        p._support_last_update_at = None
        p._avoid_side = None
        p._stage_target = None
        p._stage_start = None
        p._stage_leg = 0
        p._block_pressing = False
        p._guard_threatened = False
        p._goal_line_push = False
        p._kick_commit_until = None
        p._pass_cooldown_until = None


def _anti_idle(
    context: Context, players: list[Player], store, phase: Phase,
) -> None:
    """规则 M-05:playing 未 stopped 时,球距 ≤3m 且 10s 几乎不动 → 罚下 30s。

    宁可小幅挪动,也不能站着。方向按 phase 区分:
      - 对方定位球/开球:朝远离球方向挪(不碰球,满足 R-07"正在远离"豁免);
      - 门将:沿门线横向挪(保持 R-07 门线豁免);
      - 其余:朝球小幅前压(有压迫感,也算动)。
    """
    g = context.game
    if g is None or g.state != GameState.PLAYING or g.stopped:
        return
    ball = context.ball
    if ball is None:
        return
    now = context.now
    gk = next((p for p in players if p.id == store.gk_id), None)

    for p in players:
        if p.pose is None or p.is_kicking:
            continue
        if p._kick_commit_until is not None and now < p._kick_commit_until:
            continue
        d_ball = dist(p.pose.x, p.pose.y, ball.x, ball.y)
        if d_ball > M05_BALL_DIST_M:
            p._idle_pos = (p.pose.x, p.pose.y)
            p._idle_at = now
            continue
        lp = getattr(p, "_idle_pos", None)
        if lp is None or dist(p.pose.x, p.pose.y, lp[0], lp[1]) > M05_MOVE_M:
            p._idle_pos = (p.pose.x, p.pose.y)
            p._idle_at = now
            continue
        if now - getattr(p, "_idle_at", now) < M05_IDLE_S:
            continue

        # 强制挪动一步
        if phase in (Phase.OPP_SET_PLAY, Phase.OPP_KICKOFF,
                     Phase.OUR_SET_PLAY, Phase.OUR_KICKOFF):
            # 朝远离球的方向(径向),满足"正在远离"豁免
            a = angle_to(ball.x, ball.y, p.pose.x, p.pose.y)
            tx = p.pose.x + M05_STEP_M * math.cos(a)
            ty = p.pose.y + M05_STEP_M * math.sin(a)
        elif p is gk:
            # 沿门线横向挪:选"远离球的 y"方向,不会碰球
            sign = 1.0 if p.pose.y < ball.y else -1.0
            tx = p.pose.x
            ty = p.pose.y + sign * M05_STEP_M
        else:
            # 朝球小幅前压
            a = angle_to(p.pose.x, p.pose.y, ball.x, ball.y)
            tx = p.pose.x + M05_STEP_M * math.cos(a)
            ty = p.pose.y + M05_STEP_M * math.sin(a)

        p.action = "anti_idle"
        p.walk_to(
            _clamp_field(context, (tx, ty)),
            avoid_ball=False, avoid_robots=False, arrive_dist=0.05,
        )
        p._idle_at = now


def _clear_normal_sticky(store) -> None:
    store.normal_attacker = None


def _player_dist_to_ball(context: Context, p: Player) -> float:
    """球员到球当前位置的距离。"""
    ball = context.ball
    if ball is None or p.pose is None:
        return math.inf
    return dist(p.pose.x, p.pose.y, ball.x, ball.y) + _fallen_time_cost(p)


def _fallen_time_cost(p: Player) -> float:
    return FALLEN_COST if p.is_fallen else 0.0


def _select_closest_attacker(
    context: Context,
    players: list[Player],
    preferred_id: int | None = None,
) -> Player | None:
    """选到球距离最小的球员。

    ``preferred_id`` 是上一帧的攻击手,享受 ``ATTACKER_KEEP_DIST_MARGIN_M``
    的"卫冕优势"——挑战者必须明显更近才能上位,避免每帧换人导致原地扭动。
    """
    ranked = [(p, _player_dist_to_ball(context, p)) for p in players]
    if not ranked:
        return None
    best, best_dist = min(ranked, key=lambda item: item[1])
    preferred = next((item for item in ranked if item[0].id == preferred_id), None)
    if (
        preferred is not None
        and preferred[1] <= best_dist + ATTACKER_KEEP_DIST_MARGIN_M
    ):
        return preferred[0]
    return best


# ======================================================================
# Phase 行为
# ======================================================================


def _act_normal(context: Context, players: list[Player], store) -> None:
    """NORMAL:1-1-1 —— 门将固定,离球最近者攻击,其余支援。

    角色分派顺序很重要:**先定门将**,否则"离球最近"会把门将拉去进攻,球门空虚。

    对方控球且球在我方半场附近时:第二个非门将也压上抢(press),不能站着看。
    """
    if not players:
        return

    ball = context.ball
    gk = _pick_goalkeeper(context, players, store)

    # --- 球不可见:全员回位,别乱跑 ---
    if ball is None:
        gx, gy = own_goal(context)
        for p in players:
            if p is gk:
                p.action = "guard:home"
                p.guard()
            else:
                p.action = "no_ball"
                p.move_to_position(_clamp_field(context, (gx + 4.0, 0.0)))
        return

    field_players = [p for p in players if p is not gk]
    if not field_players:
        gk.guard()
        return

    # --- 攻击手(带迟滞) ---
    attacker = _select_closest_attacker(
        context, field_players, getattr(store, "normal_attacker", None),
    )
    if attacker is None:
        gk.guard()
        return
    store.normal_attacker = attacker.id

    # --- 防守拦截(问题 1/6)：对方控球、球在我方半场、球朝门滚 →
    # 最近者走到球后方(球→己门连线)拦截位，面朝球，而非呆在后面追球。
    opp_has = not we_have_ball(context)
    if opp_has and ball.x < 0 and store.ball_vx < -0.3:
        # 预测球到达拦截线(ball.x - INTERCEPT_AHEAD_M)时的 y
        t_intercept = (ball.x - INTERCEPT_AHEAD_M - ball.x) / store.ball_vx
        intercept_y = ball.y + store.ball_vy * t_intercept
        intercept_x = ball.x - INTERCEPT_AHEAD_M
        pt = _clamp_field(context, (intercept_x, intercept_y))
        attacker.action = "defend:intercept"
        attacker.move_to_position(pt)
    else:
        attacker.attack()

    # --- 其余人 ---
    opp_has = not we_have_ball(context)
    ball_in_press = ball.x < PRESS_ZONE_X
    for p in field_players:
        if p is attacker:
            continue
        # 对方控球且球在我方半场附近 → 第二人压上施压,但**不堆到球上**:
        # 站在球侧方 ~1.4m 处(与 attacker 错开),move_to_position 自带避球+避
        # 机器人,既施压又不会两人叠在球上互撞抢球(问题 5)。若球被撞松,这个
        # 侧翼位置正好能补抢。
        if opp_has and ball_in_press:
            side = 1.0 if (p.id % 2 == 0) else -1.0
            pt = _clamp_field(context, (ball.x - 0.3, ball.y + side * 1.4))
            p.action = "press:support"
            p.move_to_position(pt)
        else:
            p.support()

    # --- 门将 ---
    gk.guard()


def _act_our_kickoff(context: Context, players: list[Player], store) -> None:
    """OUR_KICKOFF:主罚者把球短传给接应点(不能直接射门,G-03 需 ≥2 次触球)。

    球离开中心点后,立刻转正常拼抢逻辑。
    """
    if not players:
        return

    ball = context.ball
    if ball is None:
        _act_normal(context, players, store)
        return

    # 球已离开中心 → 开球完成,转正常
    if dist(ball.x, ball.y, 0.0, 0.0) > CENTER_LEAVE_DIST_M:
        _act_normal(context, players, store)
        return

    gk = _pick_goalkeeper(context, players, store)
    takers = [p for p in players if p is not gk]
    if not takers:
        gk.guard()
        return

    # 锁定主罚者:进入开球阶段时选一次;只有原主罚者不可用(摔倒/罚下)才换人。
    # 每帧重算"离球最近"会让两人在球两侧来回横跳,谁也踢不到球。
    active_ids = {p.id for p in takers}
    if store.prev_phase != Phase.OUR_KICKOFF or store.kickoff_taker not in active_ids:
        pick = _select_closest_attacker(context, takers)
        store.kickoff_taker = pick.id if pick is not None else None

    taker = next((p for p in takers if p.id == store.kickoff_taker), None)
    if taker is None:
        taker = takers[0]
        store.kickoff_taker = taker.id

    # 接应点(球的正前方偏侧,短传目标)。前向距离用 KICKOFF_PASS_FWD_M,
    # 横向用 KICKOFF_SUPPORT_Y(已收到 1.2,配合力度不会把球带出边线)。
    side = 1.0 if ball.y >= 0.0 else -1.0
    pass_target = _clamp_field(
        context, (ball.x + KICKOFF_PASS_FWD_M, side * KICKOFF_SUPPORT_Y),
    )

    taker.action = "kickoff:taker"
    # 用开球力度 KICK_POWER_OUR_KICKOFF=5.0:朝对方球门大力踢,推进到对方半场深处。
    # G-03:开球直接进球无效不犯规(需≥2触球)。allow_goal=False 保证落点不瞄门框。
    taker.take_kickoff(pass_target, KICK_POWER_OUR_KICKOFF)

    # 接应球员:跑到接应点(中圈外,本方半场)
    support_slot = _clamp_own_half(
        context,
        (KICKOFF_SUPPORT_X, side * KICKOFF_SUPPORT_Y),
    )
    support_slot = _clamp_outside_circle(context, support_slot)
    for p in takers:
        if p is taker:
            continue
        p.action = "kickoff:support"
        p.move_to_position(support_slot)

    gk.guard()


def _act_opp_kickoff(context: Context, players: list[Player], store) -> None:
    """OPP_KICKOFF:门将守门,其余人**盯人**封堵第一脚出球线。

    规则 P-02:非开球方不得进中圈、不得越半场 —— 所以所有站位点都必须过
    ``_clamp_own_half`` + ``_clamp_out_circle_keep_y``。

    战术上不追球(球在中圈里,进去就罚下),而是:
      1. 按"离我方球门最近"给对手排序 —— 那是最可能的接球目标;
      2. 贪心匹配:每个防守者挑离自己最近、还没被盯的对手;
      3. 站到对手「靠我门侧」1.2m 处,堵住他转身往球门的出球线。
    比固定 3 个点好在:人数不确定也不会漏人,而且会跟着对手的横向移动走。
    """
    if not players:
        return

    ball = context.ball
    if ball is not None and dist(ball.x, ball.y, 0.0, 0.0) > CENTER_LEAVE_DIST_M:
        _act_normal(context, players, store)
        return

    gk = _pick_goalkeeper(context, players, store)
    rest = [p for p in players if p is not gk]

    if gk is not None:
        gk.guard()

    if not rest:
        return

    # 越靠我方球门越优先盯(x 越小 = 越靠我门)
    opps = [r for r in context.opponents.values() if r.pose is not None]
    opps.sort(key=lambda r: r.pose.x)

    r_circle = context.field.circle_radius
    used: set[int] = set()
    for p in rest:
        best_i, best_d = -1, math.inf
        if p.pose is not None:
            for i, r in enumerate(opps):
                if i in used:
                    continue
                d = dist(p.pose.x, p.pose.y, r.pose.x, r.pose.y)
                if d < best_d:
                    best_i, best_d = i, d
        if best_i < 0:
            # 没有可盯的对手 → 退到中圈后兜底(两个点错开,别叠在一起)
            idx = rest.index(p)
            tgt = (
                -r_circle - READY_BLOCK_BACK_M,
                READY_BLOCK_Y if idx % 2 == 0 else -READY_BLOCK_Y,
            )
        else:
            used.add(best_i)
            tgt = _mark_point(context, opps[best_i].pose)
        target = _clamp_out_circle_keep_y(
            context, _clamp_own_half(context, tgt),
        )
        p.action = "opp_kickoff:mark"
        p.move_to_position(target)


def _act_our_set_play(context: Context, players: list[Player], store) -> None:
    """OUR_SET_PLAY:按定位球类型执行剧本。

    主罚者绕到球后 → 踢向 ``_set_play_aim`` 给的目标;
    其余人跑接应/二点;门将守门。
    球已被踢出(离开进入时的位置) → 转正常拼抢。
    """
    if not players:
        return

    ball = context.ball
    if ball is None:
        _act_normal(context, players, store)
        return

    set_play = get_set_play_type(context)

    # 记录进入时的球位;只有在"我们已经踢出过"且球明显离开原位时才算完成(R-05),
    # 否则裁判摆球阶段会被误判成"已踢出"。
    if store.setplay_ball is None:
        store.setplay_ball = (ball.x, ball.y)
        store.setplay_kicked = False
        store.setplay_kicked_at = None
    elif store.setplay_kicked:
        # 已经踢出:球明显离开原位 → 立刻转正常;
        # 或者等了 SETPLAY_GIVEUP_S 还没踢动(踢空了) → 也转正常,
        # 否则会一直卡在定位球分支里反复踢、机器人无法走位(M-05 风险)。
        moved = dist(ball.x, ball.y, store.setplay_ball[0], store.setplay_ball[1])
        if moved > 1.2:
            _act_normal(context, players, store)
            return
        if (
            store.setplay_kicked_at is not None
            and context.now - store.setplay_kicked_at > SETPLAY_GIVEUP_S
        ):
            _act_normal(context, players, store)
            return

    gk = _pick_goalkeeper(context, players, store)
    takers = [p for p in players if p is not gk]
    if not takers:
        gk.guard()
        return

    # 锁定主罚者:只在"原主罚者不可用"时重选。每帧重算"离球最近"会让两个
    # 人在球两侧来回横跳,谁也踢不到球。
    active_ids = {p.id for p in takers}
    if store.setplay_taker not in active_ids:
        pick = _nearest_player_to(takers, ball.x, ball.y)
        store.setplay_taker = pick.id if pick is not None else None
    taker = next((p for p in takers if p.id == store.setplay_taker), None)
    if taker is None:
        taker = takers[0]
        store.setplay_taker = taker.id

    support_point = _set_play_support_point(context, set_play, ball)
    aim, power = _set_play_aim(context, set_play, ball, support_point)
    stage_m = SETPLAY_STAGE_M
    lateral_tol = KICKOFF_LATERAL_TOL

    # 能一脚直接打进就射:定位球时防守方必须退 1.45m(R-07),没人干扰,
    # 角度和力度都可以精确设计。射门需要更高的方向精度,所以绕后距离拉长、
    # 横向容差收紧(1.2m 处 0.35m = 16° 会直接打飞;2.0m 处 0.20m = 5.7°)。
    shot = _setplay_shot(context, set_play, ball, taker)
    if shot is not None:
        aim, power = shot
        stage_m = SETPLAY_SHOT_STAGE_M
        lateral_tol = SETPLAY_SHOT_LATERAL_TOL
        taker.action = f"setplay:shoot:{set_play.value.lower()}"
    else:
        taker.action = f"setplay:{set_play.value.lower()}"

    if taker.stage_behind_and_kick(
        aim, power, stage_m=stage_m, lateral_tol=lateral_tol,
        allow_goal=(shot is not None),
    ):
        if not store.setplay_kicked:
            store.setplay_kicked_at = context.now
        store.setplay_kicked = True

    for p in takers:
        if p is taker:
            continue
        p.action = "setplay:support"
        p.move_to_position(support_point)

    gk.guard()


def _opp_set_play_slots(
    context: Context, set_play: SetPlay, ball, n: int,
) -> list[tuple[float, float]]:
    """按对方定位球类型给出布防点(未经避让处理)。

    不同类型威胁完全不同,用同一套人墙是浪费:
      - 角球:球在角旗区,威胁是"传到门前抢点" → 一人堵近柱 + 其余盯抢点者
      - 界外球:威胁是"就近接应后推进" → 贴住对方接应者
      - 任意球/点球:威胁是"直接射门" → 排人墙挡射门角
      - 球门球:对方在自家门前发,没有射门威胁 → 压到中场逼出球点
    """
    f = context.field
    half_l = f.length / 2.0
    half_w = f.width / 2.0
    goal_half = f.goal_width / 2.0
    side = 1.0 if ball.y >= 0.0 else -1.0
    slots: list[tuple[float, float]] = []

    if set_play == SetPlay.CORNER_KICK:
        # 近门柱:角球从 y=+4.45 来时,近柱是 y=+1.3 那一侧
        slots.append((-half_l + 0.45, side * (goal_half - 0.30)))
        # 其余盯我方半场里离球门最近的对方抢点者
        marks = [
            r for r in context.opponents.values()
            if r.pose is not None and r.pose.x < 0.0
        ]
        marks.sort(key=lambda r: r.pose.x)
        for i in range(1, n):
            k = i - 1
            if k < len(marks):
                slots.append(_mark_point(context, marks[k].pose))
            else:
                slots.append((-half_l + 1.5, -side * 1.0))
        return slots

    if set_play == SetPlay.THROW_IN:
        # 压迫接应点:跳过离球最近的那个(那是主罚者本人)
        marks = sorted(
            [r for r in context.opponents.values() if r.pose is not None],
            key=lambda r: dist(r.pose.x, r.pose.y, ball.x, ball.y),
        )
        marks = marks[1:] if len(marks) > 1 else []
        for i in range(n):
            if i < len(marks):
                slots.append(_mark_point(context, marks[i].pose))
            else:
                slots.append((-half_l + 2.0, 0.0))
        return slots

    if set_play in (
        SetPlay.DIRECT_FREE_KICK,
        SetPlay.INDIRECT_FREE_KICK,
        SetPlay.PENALTY_KICK,
    ):
        # 人墙:站"球→己方门"连线上距球 1.65m,垂直方向排开挡射门角
        a = angle_to(ball.x, ball.y, *own_goal(context))
        base_x = ball.x + OPP_SET_AVOID_M * math.cos(a)
        base_y = ball.y + OPP_SET_AVOID_M * math.sin(a)
        perp = a + math.pi / 2.0
        for i in range(n):
            off = (i - (n - 1) / 2.0) * WALL_SPACING_M
            slots.append((
                base_x + off * math.cos(perp),
                base_y + off * math.sin(perp),
            ))
        return slots

    # 对方球门球 / 其它:压到中场逼抢出球点(对方在自家门前,没有射门威胁)
    for i in range(n):
        slots.append((
            -half_l + 3.0 + i * 1.0,
            (1.0 if i % 2 == 0 else -1.0) * 2.0,
        ))
    return slots


def _act_opp_set_play(context: Context, players: list[Player], store) -> None:
    """OPP_SET_PLAY:全员退到 1.65m 外 + 按类型布防,**绝不碰球**。

    这是规则红线(R-06 / R-07):主罚方触球前防守方距球 < 1.45m,
    罚下 30s 并重踢。所以:
      1. 先保证离球 ≥ 1.65m + 0.25m 余量(retreat_to 内部处理,
         含"径向退出 + 正在远离豁免 + 死区处理")
      2. 再按定位球类型站到各自的布防点(见 _opp_set_play_slots)
      3. 门将留在门线(规则豁免:门线防守不算避让违例)且禁止出击
    """
    if not players:
        return

    ball = context.ball
    gk = _pick_goalkeeper(context, players, store)

    if ball is None:
        # 球不可见:原地不动最安全(乱走可能撞上对方摆的球)
        for p in players:
            p.action = "opp_setplay:hold"
            p.stop()
        return

    set_play = get_set_play_type(context)
    rest = [p for p in players if p is not gk]

    if rest:
        slots = _opp_set_play_slots(context, set_play, ball, len(rest))
        for p, slot in zip(rest, slots):
            # 离球不够远时,retreat_to 会先径向退出,不碰球
            p.retreat_to(_clamp_field(context, slot), OPP_SET_AVOID_M)

    # 门将:贴门线 + 禁止出击。
    #   贴门线 → 命中 R-07 的"门线防守"豁免(站门线前 0.6m 不算豁免区);
    #   禁止出击 → 否则球在禁区里时门将会冲上去碰球,R-06 防守方先触球罚下 30s。
    if gk is not None:
        gk.guard(allow_rush=False, on_line=True)


def _act_ready(context: Context, players: list[Player], store) -> None:
    """READY:各自走到预设阵型位。

    站位必须**提前满足 SET 的合法性**(本方半场 / 非开球方在中圈外),
    否则 SET 一开始就要移动,触发 M-01(宽限后 1s 内位移 > 0.15m 罚下)。
    """
    if not players:
        return

    game = context.game
    our_kickoff = game is not None and game.kicking_team == context.team_id
    f = context.field
    gk = _pick_goalkeeper(context, players, store)

    # 门将:小禁区中心
    if gk is not None:
        gk.action = "ready:gk"
        gk.move_to_position(
            _clamp_own_half(
                context, (-f.length / 2.0 + f.goal_area_length / 2.0, 0.0),
            ),
        )

    rest = [p for p in players if p is not gk]

    if our_kickoff:
        # 我方开球:主罚者站中圈内靠近球(合法),另一个拉边接应
        slots = [
            (-f.circle_radius + READY_TAKER_BACK_M, 0.0),  # 中圈内，可进
            (READY_SUPPORT_X, READY_SUPPORT_Y),            # 接应
            (READY_SUPPORT_X, -READY_SUPPORT_Y),
        ]
        for i, p in enumerate(rest):
            tgt = slots[i] if i < len(slots) else slots[-1]
            if i == 0:
                # 开球方允许在中圈内,但仍须本方半场
                target = _clamp_own_half(context, tgt)
            else:
                target = _clamp_outside_circle(
                    context, _clamp_own_half(context, tgt),
                )
            p.action = "ready:taker" if i == 0 else "ready:support"
            p.move_to_position(target)
    else:
        # 对方开球:全员本方半场 + 中圈外
        slots = [
            (-f.circle_radius - READY_BLOCK_BACK_M, 0.0),
            (-f.circle_radius - READY_BLOCK_BACK_M, READY_BLOCK_Y),
            (-f.circle_radius - READY_BLOCK_BACK_M, -READY_BLOCK_Y),
        ]
        for i, p in enumerate(rest):
            tgt = slots[i] if i < len(slots) else slots[-1]
            p.action = "ready:block"
            p.move_to_position(
                _clamp_outside_circle(context, _clamp_own_half(context, tgt)),
            )


# ======================================================================
# 战场可视化 —— 显示球位置 + 球员到球的距离,画到 ROS 可视化
# ======================================================================


def _draw_teammate_marker(p: Player) -> None:
    """我方队员可视化:红色。踢球中→方块,否则→球体。

    每帧对所有球员统一调用(不受 phase/判罚/就绪影响)。标签两行:
    - 上:编号 + 当前高层动作(``p.action``),踢球中追加 ``[KICK]``。
    - 通过形状(方块 vs 球体)再次区分是否进入 kick 状态。
    """
    from .framework import debugdraw

    if p.pose is None:
        return
    red = (1.0, 0.2, 0.2)
    if p.is_kicking:
        debugdraw.cube(p.pose.x, p.pose.y, rgb=red, scale=0.38, ns="teammate")
    else:
        debugdraw.point(p.pose.x, p.pose.y, rgb=red, scale=0.3, ns="teammate")
    kick_tag = " [KICK]" if p.is_kicking else ""
    label = f"{p.id}:{p.action}{kick_tag}"
    debugdraw.text(p.pose.x, p.pose.y, label, rgb=(1.0, 0.9, 0.6), ns="teammate_id")


def _analyze_and_draw(context: Context, players: list[Player], store) -> None:
    """每帧:计算球员到球的距离,画可视化。"""
    from .framework import debugdraw

    ball = context.ball
    if ball is None:
        return

    # 1. 画球当前位置(绿色点)
    debugdraw.point(ball.x, ball.y, rgb=(0.0, 1.0, 0.0), scale=0.2, ns="ball_current")

    # 2. 球员到球的距离:我方(红标签)+ 敌方(蓝标签)
    for p in players:
        if p.pose is None:
            continue
        d = dist(p.pose.x, p.pose.y, ball.x, ball.y)
        debugdraw.text(
            p.pose.x + 0.3, p.pose.y - 0.3, f"{d:.1f}m",
            rgb=(1.0, 0.6, 0.6), ns="dist_ours",
        )
    for r in context.opponents.values():
        if r.pose is None:
            continue
        d = dist(r.pose.x, r.pose.y, ball.x, ball.y)
        debugdraw.text(
            r.pose.x + 0.3, r.pose.y - 0.3, f"{d:.1f}m",
            rgb=(0.6, 0.6, 1.0), ns="dist_opp",
        )
