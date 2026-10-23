"""Player:单个机器人的控制 handle —— 用户可直接编辑这个类。

平台原语(set_velocity / kick / release_kick / request_mode / get_up)委托给注入的
``_backend``(framework 层);状态 property(pose / mode / is_fallen / penalty)从
``self.context`` 或 backend 读。

本文件包含三层动作:
- 走位原语:walk_to / face_to / move_to_position / ensure_ready
- 技术动作:push_ball(五模式推球) / attack(射门 or 推球) / guard(门将) /
           support(支援) / defend(防守滑步) / stage_behind_and_kick(绕后踢) /
           retreat_to(定位球避让)
- 底层:set_velocity / kick / release_kick

战术来源:五模式连续推球移植自 motrixarena-S2 的 ContinuousPushController,
参数已按 30Hz + 本机型尺寸重调(见 param.py 第 3 节)。

实例跨整场比赛存活;``self.context`` 每帧被框架覆写。
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING

from .framework.types import Context, Penalty, Pose2D
from .framework import debugdraw

from .param import *
from .utils.geom import (
    angle_to,
    clamp,
    dist,
    normalize_angle,
    opponent_goal,
    own_goal,
    own_goal_area_center,
)
from .utils.obstacles import collect_obstacles
from .utils.path_planner import plan_global_path

if TYPE_CHECKING:
    from .framework.config import SoccerConfig


__all__ = ["Player", "match_mode", "we_have_ball"]


_log = logging.getLogger(__name__)


# ======================================================================
# 模块级工具函数
# ======================================================================


def we_have_ball(context: Context) -> bool:
    """我方是否控球:我方最近者比对方最近者明显更近。

    ``POSSESSION_MARGIN_M`` 是"明显"的阈值,防止两边距离接近时来回切换。
    球不可见时返回 False(宁可当没控球,先回防)。
    """
    ball = context.ball
    if ball is None:
        return False
    ours = [
        dist(r.pose.x, r.pose.y, ball.x, ball.y)
        for r in context.teammates.values()
        if r.pose is not None
    ]
    theirs = [
        dist(r.pose.x, r.pose.y, ball.x, ball.y)
        for r in context.opponents.values()
        if r.pose is not None
    ]
    if not ours:
        return False
    best_ours = min(ours)
    best_theirs = min(theirs) if theirs else math.inf
    return best_ours < best_theirs - POSSESSION_MARGIN_M


def match_mode(context: Context) -> str:
    """比分 + 剩余时间 → 比赛态势。不新增 Phase,只影响 NORMAL 里的门槛。

    返回:
      ``"allin"``  终场落后 → 全员压上搏命
      ``"park"``   领先(含大比分领先) → 收缩死守
      ``"press"``  落后(非终场) → Support 提前压上
      ``"hold"``   领先 1 球(非终场) → 适度收缩
      ``"steady"`` 终场平局 → 维持标准站位,不冒险
      ``"normal"`` 其余(含读不到比赛信息)

    读不到 ``context.game`` 时一律返回 ``"normal"``——宁可打标准阵型,
    也不要因为误读比分把全队压上导致空门。
    """
    game = context.game
    if game is None:
        return "normal"
    secs = game.secs_remaining
    if secs <= 0:                      # 未开赛 / 已结束 / 字段无效
        return "normal"
    my = game.get_team_state(context.team_id)
    opp = next(
        (t for t in game.teams if t.team_number != context.team_id), None,
    )
    if my is None or opp is None:
        return "normal"
    diff = my.score - opp.score

    if secs <= ENDGAME_SECS:
        if diff >= HOLD_SCORE_DIFF:
            return "park"
        if diff <= PRESS_SCORE_DIFF:
            return "allin"
        return "steady"

    if diff >= PARK_SCORE_DIFF:
        return "park"
    if diff >= HOLD_SCORE_DIFF:
        return "hold"
    if diff <= PRESS_SCORE_DIFF:
        return "press"
    return "normal"


def _heading_clearance(
    px: float, py: float, heading: float, obstacles: list,
) -> float:
    """用于局部避障。沿 (px,py) 朝 ``heading`` 的 lookahead 射线,到最近障碍的余量(dist - radius)。

    只考虑**前方**(投影 t>0)的障碍;身后 / 侧后的障碍不挡这个方向,跳过——否则贴近
    任何障碍(哪怕正后方)都会把所有方向的余量拉低,误判"全堵"。
    无障碍返回 inf;越大越空,负值表示会撞。
    """
    ux, uy = math.cos(heading), math.sin(heading)
    min_clear = math.inf
    for obs in obstacles:
        t = (obs.x - px) * ux + (obs.y - py) * uy
        if t <= 0.0:
            continue                      # 障碍在身后/侧后,不挡此方向
        if t > PLAN_LOOKAHEAD:
            t = PLAN_LOOKAHEAD
        nx, ny = px + ux * t, py + uy * t
        clear = math.hypot(obs.x - nx, obs.y - ny) - obs.radius
        if clear < min_clear:
            min_clear = clear
    return min_clear


def _behind_ball(
    ball_x: float, ball_y: float, aim: tuple[float, float], offset: float,
) -> tuple[float, float]:
    """球在 球→``aim`` 连线上的"后方"点:从球沿【背离 aim】方向退 ``offset`` 米。

    追球走位目标用它:球落在机器人与 ``aim`` 之间,到位时天然对准 aim 方向。
    aim 与球重合(退化)时,方向不定,直接返回球位。
    """
    dx, dy = aim[0] - ball_x, aim[1] - ball_y
    d = math.hypot(dx, dy)
    if d < 1e-6:
        return (ball_x, ball_y)
    ux, uy = dx / d, dy / d              # 球指向 aim 的单位向量
    return (ball_x - ux * offset, ball_y - uy * offset)   # 背离 aim 退 offset


def _soft_clip(value: float, threshold: float) -> float:
    """软限幅:低于 threshold 线性通过;超过后 tanh 压缩,输出严格落在 (-1, 1)。

    硬 clip 会让指令长时间顶在饱和值 → 过冲 → 振荡。软限幅"超调越大压得越狠",
    自带阻尼。移植自参考项目,用于 vy / w。
    """
    if abs(value) <= threshold:
        return value
    excess = min((abs(value) - threshold) / (1.0 - threshold + 1e-9), 6.0)
    return math.copysign(
        threshold + (1.0 - threshold) * math.tanh(excess), value,
    )


def _point_segment_distance(
    px: float, py: float, ax: float, ay: float, bx: float, by: float,
) -> float:
    """点 (px,py) 到线段 a→b 的距离。用于射门线净空判定。"""
    vx, vy = bx - ax, by - ay
    length2 = vx * vx + vy * vy
    if length2 < 1e-9:
        return math.hypot(px - ax, py - ay)
    t = clamp(((px - ax) * vx + (py - ay) * vy) / length2, 0.0, 1.0)
    return math.hypot(px - (ax + vx * t), py - (ay + vy * t))


def in_field(
    context: Context, x: float, y: float, margin: float = 0.0,
) -> bool:
    """点是否在场地内(可要求离边线/底线 ``margin`` 米)。"""
    half_l = context.field.length / 2.0
    half_w = context.field.width / 2.0
    return abs(x) <= half_l - margin and abs(y) <= half_w - margin


def legal_kick_target(
    context: Context,
    target: tuple[float, float],
    *,
    margin: float = KICK_AIM_MARGIN_M,
    allow_goal: bool = True,
) -> tuple[float, float]:
    """把踢球落点夹成"不会把球踢出界"的合法点。

    实测问题:球门被人挡住时,把球往边线方向大脚踢,球滚出边线 → R-01 判给
    对方界外球。一次解围白送一个定位球,比被断还亏(被断至少球还在场内)。

    规则:
      - **射门落点**(在门框内、x 在底线之外)原样保留 —— 打偏出底线是射门的
        正常代价,不能为了"不出去"把射门也改掉;
      - 其它落点一律夹到距边线/底线 ≥ ``margin`` 的场内区域。
    """
    half_l = context.field.length / 2.0
    half_w = context.field.width / 2.0
    x, y = target
    if allow_goal and x >= half_l and abs(y) <= context.field.goal_width / 2.0:
        return (x, y)
    return (
        clamp(x, -half_l + margin, half_l - margin),
        clamp(y, -half_w + margin, half_w - margin),
    )


# ======================================================================
# Player
# ======================================================================


class Player:
    """单个球员的 handle。可以在这里加入新的技术动作。"""

    def __init__(
        self,
        player_id: int,
        config: "SoccerConfig",
        _backend: object | None,
    ) -> None:
        self.id: int = player_id
        self.config: "SoccerConfig" = config
        self._backend = _backend           # 机器人控制接口的包装，通常不用改
        self.context: Context | None = None

        # 当前高层动作名(仅供可视化/调试)。策略分派层每帧写入;有子状态的动作
        # (如 guard / push_ball)在方法内部细化。可视化 pass 读它标注文字,见 main.py。
        self.action: str = "init"

        # SDK 缓存字段,框架后台会自动更新
        self._mode: str | None = None
        self._fall_down_state: str | None = None

        # 避障绕行侧记忆(跨帧;None=当前无绕行)
        self._avoid_side: float | None = None

        # 踢球迟滞状态(跨帧)
        self._kicking: bool = False

        # block/guard 的跨帧迟滞状态
        self._block_pressing: bool = False
        self._guard_threatened: bool = False
        # 门线撞球进门迟滞状态
        self._goal_line_push: bool = False
        # support 卡住后临时转 attack 的跨帧状态
        self._support_last_pos: tuple[float, float] | None = None
        self._support_stationary_since: float | None = None
        self._support_last_update_at: float | None = None

        # --- 推球控制器的跨帧状态 ---
        # 上一帧的模式(用于迟滞:ORBIT_BEHIND / LATERAL_ALIGN 的退出比进入更难)
        self._push_mode: str | None = None

        # --- 球速估计(门将预判用) ---
        self._ball_prev: tuple[float, float] | None = None

        # --- 踢球提交窗口:踢出后这段时间内不允许 release_kick,否则动作被打断 ---
        self._kick_commit_until: float | None = None

        # --- 传球冷却:防止两名队友来回对传(PASS_COOLDOWN_S) ---
        self._pass_cooldown_until: float | None = None

        # --- 起身节流:上一次下发 get_up 的时刻(见 ensure_ready) ---
        self._get_up_at: float | None = None

        # --- 切模式节流:上一次下发 request_mode 的时刻与目标(见 request_mode) ---
        self._mode_req_at: float | None = None
        self._mode_req_target: str | None = None

        # --- 速度指令限幅:上一帧真正下发的速度与时刻(见 _limit_velocity) ---
        self._cmd_vel: tuple[float, float, float] = (0.0, 0.0, 0.0)
        self._cmd_at: float | None = None

        # --- 绕后踢球的计时(定位球兜底用) ---
        self._stage_target: tuple[float, float] | None = None
        self._stage_start: float | None = None
        # 场外绕行的分段(0=先到过渡点,1=再去场外站位点)。用状态而不是
        # "每帧算直线净空"来决定走哪段:后者在临界值附近会逐帧翻转,机器人原地抖。
        self._stage_leg: int = 0

    # ------------------------------------------------------------------
    # 状态读取
    # ------------------------------------------------------------------

    @property
    def is_kicking(self) -> bool:
        """当前是否处于踢球状态。"""
        return self._kicking

    @property
    def pose(self) -> Pose2D | None:
        ctx = self.context
        if ctx is None:
            return None
        robot = ctx.teammates.get(self.id)
        return None if robot is None else robot.pose

    @property
    def mode(self) -> str | None:
        if self._backend is not None:
            return self._backend.mode
        return self._mode

    @property
    def is_fallen(self) -> bool:
        return self.fall_down_state not in (None, "normal")

    @property
    def fall_down_state(self) -> str | None:
        if self._backend is not None:
            return getattr(self._backend, "fall_down_state", None)
        return self._fall_down_state

    @property
    def penalty(self) -> Penalty:
        ctx = self.context
        if ctx is None or ctx.game is None:
            return Penalty.NONE
        state = ctx.game.get_player_state(self.config.team_id, self.id)
        return Penalty.NONE if state is None else state.penalty

    @property
    def is_penalized(self) -> bool:
        return self.penalty != Penalty.NONE

    # ------------------------------------------------------------------
    # 底盘控制
    # ------------------------------------------------------------------

    def set_velocity(self, vx: float, vy: float, vyaw: float) -> None:
        """底盘速度。带**变化率限幅**(阶跃 → 斜坡),见 ``_limit_velocity``。"""
        vx, vy, vyaw = self._limit_velocity(vx, vy, vyaw)
        if self._backend is None:
            _log.debug(
                "player %d set_velocity vx=%.3f vy=%.3f vyaw=%.3f (no backend)",
                self.id, vx, vy, vyaw,
            )
            return
        self._backend.set_velocity(vx, vy, vyaw)

    def _limit_velocity(
        self, vx: float, vy: float, vyaw: float,
    ) -> tuple[float, float, float]:
        """把速度指令的变化率限幅。

        **这是"开局就倒"的直接原因之一。** 第一帧算出目标速度就直接下发
        ``MAX_LINEAR``(0 → 1.4 m/s 只用一帧),角速度同时顶到 1.5 rad/s。
        步态控制器跟不上这种突变就翻。按 ``VEL_ACC_MAX`` / ``VEL_ACC_YAW_MAX``
        限幅后,起步变成 0.4s 左右的斜坡。

        注意 ``stop()`` 不走这里:急停必须是立即归零(规则 M-01/M-02 要求
        SET/STOPPED 阶段静止),不能还在斜坡上滑。
        """
        ctx = self.context
        now = ctx.now if ctx is not None else None
        last_at = self._cmd_at
        if now is None or last_at is None:
            dt = VEL_CMD_DT_DEFAULT
        else:
            dt = now - last_at
        # 卡帧/首帧时 dt 可能异常(<=0 或很大),夹到合理区间再算增量
        dt = min(max(dt, 1.0 / 120.0), 0.20)
        self._cmd_at = now

        px, py, pw = self._cmd_vel
        # 双保险:加速度限幅(dt 相关) + 单次步长硬上限(dt 无关)。
        # 后者防的是"踢球期间不下发速度,恢复时 dt 变大 → 增量被放大"。
        d_lin = min(VEL_ACC_MAX * dt, VEL_STEP_MAX)
        d_yaw = min(VEL_ACC_YAW_MAX * dt, VEL_STEP_MAX_YAW)
        vx = clamp(px + clamp(vx - px, -d_lin, d_lin), -MAX_LINEAR, MAX_LINEAR)
        vy = clamp(py + clamp(vy - py, -d_lin, d_lin), -MAX_LINEAR, MAX_LINEAR)
        vyaw = clamp(pw + clamp(vyaw - pw, -d_yaw, d_yaw),
                     -MAX_ANGULAR, MAX_ANGULAR)
        self._cmd_vel = (vx, vy, vyaw)
        return vx, vy, vyaw

    def stop(self) -> None:
        """立即停车(不走斜坡)。SET/STOPPED 静止、摔倒清速度都靠它。"""
        self.release_kick()
        self._cmd_vel = (0.0, 0.0, 0.0)
        ctx = self.context
        self._cmd_at = ctx.now if ctx is not None else None
        if self._backend is None:
            return
        self._backend.set_velocity(0.0, 0.0, 0.0)


    # ------------------------------------------------------------------
    # 踢球
    # ------------------------------------------------------------------

    def kick(
        self,
        kick_direction: float | None = None,
        power: float = KICK_POWER_DEFAULT,
    ) -> None:
        """踢球。``kick_direction`` 是**场地坐标系**的方向角(弧度)。

        注意:踢球技能会独占底盘(backend 在 kicking 期间忽略 set_velocity),
        所以要么踢、要么走,不能同时。想恢复走位先 ``release_kick()``。
        """
        pose = self.pose
        if pose is None:
            _log.warning("player %d kick skipped: pose unknown", self.id)
            return

        ball = self.context.ball if self.context is not None else None
        if ball is None:
            _log.warning("player %d kick skipped: ball unknown", self.id)
            return

        if kick_direction is None:
            kick_plan = self.plan_kick()
            if kick_plan is None:
                _log.warning("player %d kick skipped: kick plan unavailable", self.id)
                return
            kick_direction, power = kick_plan

        if self._backend is None:
            _log.debug(
                "player %d kick ball=(%.3f, %.3f) dir=%.3f (no backend)",
                self.id, ball.x, ball.y, kick_direction,
            )
            return
        # 场地坐标 → 体坐标(用当前 pose)
        dx = ball.x - pose.x
        dy = ball.y - pose.y
        cos_t = math.cos(pose.theta)
        sin_t = math.sin(pose.theta)
        ball_x_body = dx * cos_t + dy * sin_t
        ball_y_body = -dx * sin_t + dy * cos_t
        kick_direction = normalize_angle(kick_direction)
        direction_body = normalize_angle(kick_direction - pose.theta)
        power_clamped = max(KICK_POWER_MIN, min(KICK_POWER_MAX, power))

        # --- 踢球前刹车 ---
        # 高速冲刺中踢球 = 单腿支撑 + 大幅摆腿 → 摔。实测门将大脚解围后倒地
        # （摔了就是 M-04 罚下 30s，比这一脚球值钱得多）。所以只要当前指令
        # 速度还不小，就先把速度压到 0 再起脚。
        now = self.context.now if self.context is not None else 0.0
        if math.hypot(self._cmd_vel[0], self._cmd_vel[1]) > KICK_BRAKE_SPEED:
            self._cmd_vel = (0.0, 0.0, 0.0)
            self._cmd_at = now
            if self._backend is not None:
                self._backend.set_velocity(0.0, 0.0, 0.0)

        self._kicking = True
        # 提交窗口:踢球动作约 0.5s,期间 _should_shoot 无条件保持"踢球",
        # 防止下一帧判断翻转后 push_ball/walk_to 把动作 release 掉(白踢一脚)。
        self._kick_commit_until = now + KICK_COMMIT_S
        self._backend.kick(direction_body, power_clamped, ball_x_body, ball_y_body)

    def kick_toward(
        self,
        target: tuple[float, float],
        power: float = KICK_POWER_DEFAULT,
        *,
        allow_goal: bool = False,
    ) -> None:
        """把球踢向场地上任意一点(所有传球/解围/射门的统一入口)。

        落点会过 ``legal_kick_target``:非射门落点一律夹进场内,避免把球踢出
        边线白送对方一个定位球(R-01/R-02)。射门时传 ``allow_goal=True``,
        球门内的落点原样保留。
        """
        ctx = self.context
        ball = ctx.ball if ctx is not None else None
        if ctx is None or ball is None:
            _log.warning("player %d kick_toward skipped: ball unknown", self.id)
            return
        target = legal_kick_target(ctx, target, allow_goal=allow_goal)
        self.kick(angle_to(ball.x, ball.y, target[0], target[1]), power)

    def plan_kick(self) -> tuple[float, float] | None:
        """计算踢球方向和力度(默认瞄对方球门中心)。"""
        ctx = self.context
        ball = ctx.ball if ctx is not None else None
        if ctx is None or ball is None:
            return None

        kick_target = opponent_goal(ctx)
        kick_direction = angle_to(ball.x, ball.y, *kick_target)
        kick_target = self._goal_target_for_direction(kick_direction)
        kick_power = (
            KICK_POWER_BACKFIELD if self._in_backfield()
            else KICK_POWER_DEFAULT
        )

        self._draw_kick_target(kick_target)
        return kick_direction, kick_power

    def _goal_target_for_direction(
        self, kick_direction: float,
    ) -> tuple[float, float]:
        """把射门方向投到对方门线上,用于可视化踢球目标。"""
        ctx = self.context
        ball = ctx.ball if ctx is not None else None
        if ctx is None or ball is None:
            return (0.0, 0.0)

        dx = math.cos(kick_direction)
        if dx <= 1e-6:
            return opponent_goal(ctx)
        goal_x = ctx.field.length / 2.0
        t = max(0.0, (goal_x - ball.x) / dx)
        return (goal_x, ball.y + math.sin(kick_direction) * t)

    def _in_backfield(self) -> bool:
        """球在我方后场时,默认踢球加大力度。"""
        ctx = self.context
        ball = ctx.ball if ctx is not None else None
        if ctx is None or ball is None:
            return False
        return ball.x < 0

    def _draw_kick_target(self, target: tuple[float, float]) -> None:
        """以 X 标出 plan_kick 选择的踢球目标。"""
        x, y = target
        s = KICK_TARGET_MARK_SIZE_M
        debugdraw.line(
            [(x - s, y - s), (x + s, y + s)],
            rgb=(1.0, 0.0, 1.0), ns="kick_target",
        )
        debugdraw.line(
            [(x - s, y + s), (x + s, y - s)],
            rgb=(1.0, 0.0, 1.0), ns="kick_target",
        )

    def release_kick(self) -> None:
        was_kicking = self._kicking
        self._kicking = False  # 清除踢球迟滞标志(取消方块显示)
        self._kick_commit_until = None
        if self._backend is None:
            _log.debug("player %d release_kick (no backend)", self.id)
            return
        self._backend.release_kick()
        if was_kicking:
            # 踢球期间底盘被 kick 独占,机器人实际上已经停住/减速。这里显式
            # 发一次零速度,并把斜坡起点归零,让"踢完 → 静止 → 重新起步"成为
            # 一个明确序列。若只归零斜坡起点而不发零速度:底盘仍留着踢球前的
            # 旧速度(比如 -0.37),下一帧指令一变就是一次突兀的阶跃。
            # 注意必须**只在真正结束踢球时**做:walk_to 每帧都调 release_kick,
            # 无条件归零会让斜坡永远爬不起来(速度卡在 0.117)。
            self._cmd_vel = (0.0, 0.0, 0.0)
            ctx = self.context
            self._cmd_at = ctx.now if ctx is not None else None
            self._backend.set_velocity(0.0, 0.0, 0.0)

    def kick_can_score(self, kick_direction: float) -> bool:
        """判断从当前球位按 ``kick_direction`` 踢,直线轨迹是否能进对方球门。"""
        ctx = self.context
        ball = ctx.ball if ctx is not None else None
        if ctx is None or ball is None:
            return False

        goal_x = ctx.field.length / 2.0
        dx = math.cos(kick_direction)
        dy = math.sin(kick_direction)
        if dx <= 1e-6:
            return False

        half_goal = ctx.field.goal_width / 2.0
        if half_goal <= 0.0:
            return False
        if ball.x >= goal_x:
            y_at_goal = ball.y
        else:
            t = (goal_x - ball.x) / dx
            if t < 0.0:
                return False
            y_at_goal = ball.y + dy * t
        return -half_goal <= y_at_goal <= half_goal

    # ------------------------------------------------------------------
    # 慢操作（同步接口异步调取，使得不阻塞）
    # ------------------------------------------------------------------

    def request_mode(self, mode: str) -> None:
        """切 SDK 模式。**带时间节流** —— 这是"开局就倒"的直接原因之一。

        框架的 ``request_mode`` 只做"目标模式相同则短路",没有时间节流;而
        切模式是异步的(``set_mode`` 下发后 SDK 要 ~1s 才真正进入 walk)。
        这期间后台 ``_poll_mode`` 每轮回报的还是旧模式 → 上层每帧都判定
        "还没到 walk" → 每帧都重新 ``set_gait("soccer")`` + ``set_mode("walk")``。
        等于把步态控制器反复重置,机器人刚起步就被打断 —— 表现就是开局倒地。

        这里用 ``MODE_RETRY_S`` 节流:同一目标模式在间隔内只下发一次。
        """
        ctx = self.context
        now = ctx.now if ctx is not None else None
        if (
            self._mode_req_target == mode
            and self._mode_req_at is not None
            and now is not None
            and now - self._mode_req_at < MODE_RETRY_S
        ):
            return
        self._mode_req_target = mode
        self._mode_req_at = now
        if self._backend is None:
            _log.debug("player %d request_mode -> %s (no backend)", self.id, mode)
            return
        self._backend.request_mode(mode)


    def get_up(self) -> None:
        if self._backend is None:
            _log.debug("player %d get_up (no backend)", self.id)
            return
        self._backend.get_up()

    # ------------------------------------------------------------------
    # 走位
    # ------------------------------------------------------------------

    def ensure_ready(self) -> bool:
        """活性自理:摔倒→起身,非 walk→切模式。返回本帧是否可执行动作。

        起身慢的三个真实原因(都踩过):

        1. **正在起身也算"摔倒"。** SDK 的 ``fall_down_state`` 里 ``getting_up``
           不等于 ``normal``,所以 ``is_fallen`` 为真。如果每帧都调 ``get_up()``,
           等于不停重启起身动作 —— 表现就是"一直爬不起来"。这里看到
           ``get_up`` / ``getting`` / ``rising`` 之类的状态就只等着,不下发。

        2. **重试间隔比动作还短。** 框架的 ``get_up`` 有 1s 节流,但起身动作本身
           要 1.5~2.5s。1s 一次重试同样是在打断自己。这里用 ``GET_UP_RETRY_S``
           做二次节流,保证一次动作能跑完。

        3. **摔倒时残留速度还在。** 上一条 ``set_velocity`` 会一直留在底盘上,
           机器人一边躺一边蹬腿,既起不来也可能再摔一次。必须显式 ``stop()``
           (参考实现的行为树里同样发 ``StopPlayer "fall down recovery"``)。
        """
        ctx = self.context
        now = ctx.now if ctx is not None else 0.0
        state = self.fall_down_state
        fallen = state not in (None, "normal")

        if fallen:
            self.stop()                     # 清掉残留速度,别边躺边蹬腿
            low = str(state).lower()
            if "get_up" in low or "getting" in low or "rising" in low:
                return False                # 正在起身:让它站完,别打断
            last = self._get_up_at
            if last is None or now - last >= GET_UP_RETRY_S:
                self._get_up_at = now
                _log.info("player %d get_up (state=%s)", self.id, state)
                self.get_up()
            return False

        # 刚下发过起身:等它站稳再切 walk,否则 set_mode 会把起身动作打断
        if self._get_up_at is not None:
            if now - self._get_up_at < GET_UP_SETTLE_S:
                return False
            self._get_up_at = None

        if self.mode != "walk":
            self.request_mode("walk")
            return False
        return True

    def face_to(self, target_theta: float) -> None:
        """原地转向到目标朝向。"""
        if self.pose is None:
            self.stop()
            return
        err = normalize_angle(target_theta - self.pose.theta)
        self.set_velocity(0.0, 0.0, self._angular(err))

    def walk_to(
        self,
        target: tuple[float, float],
        *,
        face: float | None = None,
        avoid_ball: bool = False,
        avoid_robots: bool = False,
        arrive_dist: float = ARRIVE_DIST,
    ) -> bool:
        """走向目标点。返回是否已到达。

        避障(局部规划器,简化版 VFH):``avoid_ball`` / ``avoid_robots`` 开启时,把
        球 / 机器人 / 球门结构收集成圆形障碍,在机器人周围扫一圈候选方向,选"最朝
        目标 + 前方 ``PLAN_LOOKAHEAD`` 内不撞障碍"的方向前进。

        行走两种模式:近距离全向,远距离转身-走。``face`` 指定到达/近距离时的朝向。
        """
        self.release_kick() # 踢球状态下，走位会被覆盖，先取消踢球状态
        pose = self.pose
        if pose is None:
            self.stop()
            return False

        tx, ty = target
        dx = tx - pose.x
        dy = ty - pose.y
        distance = math.hypot(dx, dy)

        if distance < arrive_dist:
            # 已到达:按需转到目标朝向
            if face is not None:
                err = normalize_angle(face - pose.theta)
                if abs(err) > 0.1:
                    self.set_velocity(0.0, 0.0, self._angular(err))
                else:
                    self.stop()
            else:
                self.stop()
            return True

        # 规划:默认全局 A*,找不到路时退回旧局部规划。
        goal_dir = math.atan2(dy, dx)
        planned_path: list[tuple[float, float]] | None = None
        waypoint: tuple[float, float] | None = None
        if (avoid_ball or avoid_robots) and self.context is not None:
            obstacles = collect_obstacles(
                self.context, self.id,
                ball=avoid_ball, robots=avoid_robots,
                goals=(avoid_ball or avoid_robots),
            )
            if USE_GLOBAL_PATH_PLANNER:
                planned_path = plan_global_path(
                    self.context,
                    (pose.x, pose.y),
                    (tx, ty),
                    obstacles,
                )
                if planned_path is not None:
                    waypoint = self._path_waypoint(pose, planned_path)
                    heading = angle_to(pose.x, pose.y, waypoint[0], waypoint[1])
                else:
                    heading = self._plan_heading(pose, goal_dir, obstacles)
            else:
                heading = self._plan_heading(pose, goal_dir, obstacles)
        else:
            heading = goal_dir

        # 可视化:目标点(绿)、到目标连线(灰)、规划朝向(黄箭头)、前方探测射线(青)
        debugdraw.point(tx, ty, rgb=(0.0, 1.0, 0.0), scale=0.15, ns="target")
        debugdraw.line([(pose.x, pose.y), (tx, ty)], rgb=(0.4, 0.4, 0.4), ns="to_target")
        if planned_path is not None and len(planned_path) >= 2:
            debugdraw.line(planned_path, rgb=(0.2, 0.8, 1.0), ns="global_path")
        if waypoint is not None:
            debugdraw.point(
                waypoint[0], waypoint[1],
                rgb=(0.2, 0.8, 1.0), scale=0.12, ns="global_waypoint",
            )
        debugdraw.arrow(
            pose.x, pose.y,
            pose.x + math.cos(heading) * 0.6, pose.y + math.sin(heading) * 0.6,
            rgb=(1.0, 1.0, 0.0), ns="heading",
        )
        debugdraw.line(
            [(pose.x, pose.y),
             (pose.x + math.cos(heading) * PLAN_LOOKAHEAD,
              pose.y + math.sin(heading) * PLAN_LOOKAHEAD)],
            rgb=(0.0, 0.8, 0.8), ns="lookahead",
        )

        if distance <= OMNI_DIST:
            # 近距离:全向行走,沿 heading 平移,同时转向 face
            wdx, wdy = math.cos(heading) * distance, math.sin(heading) * distance
            cos_t, sin_t = math.cos(pose.theta), math.sin(pose.theta)
            vx = LINEAR_GAIN * (wdx * cos_t + wdy * sin_t)
            vy = LINEAR_GAIN * (-wdx * sin_t + wdy * cos_t)
            speed = math.hypot(vx, vy)
            if speed > MAX_LINEAR:
                vx *= MAX_LINEAR / speed
                vy *= MAX_LINEAR / speed
            vyaw = (
                self._angular(normalize_angle(face - pose.theta))
                if face is not None else 0.0
            )
            self.set_velocity(vx, vy, vyaw)
        else:
            # 远距离:转身-走-转身,朝 heading
            angle_err = normalize_angle(heading - pose.theta)
            if abs(angle_err) > TURN_THRESHOLD:
                self.set_velocity(0.0, 0.0, self._angular(angle_err))
            else:
                vx = clamp(
                    LINEAR_GAIN * distance * math.cos(angle_err), 0.0, MAX_LINEAR,
                )
                self.set_velocity(vx, 0.0, self._angular(angle_err))
        return False

    def _path_waypoint(
        self, pose: Pose2D, path: list[tuple[float, float]],
    ) -> tuple[float, float]:
        """Pick a short lookahead waypoint from a planned global path."""
        if not path:
            return (pose.x, pose.y)
        prev = (pose.x, pose.y)
        points = path[1:] if len(path) > 1 else path
        for point in points:
            seg_len = dist(prev[0], prev[1], point[0], point[1])
            if seg_len >= GLOBAL_PATH_LOOKAHEAD_M:
                ratio = GLOBAL_PATH_LOOKAHEAD_M / max(seg_len, 1e-6)
                return (
                    prev[0] + (point[0] - prev[0]) * ratio,
                    prev[1] + (point[1] - prev[1]) * ratio,
                )
            prev = point
        return path[-1]

    def _plan_heading(
        self, pose: Pose2D, goal_dir: float, obstacles: list,
    ) -> float:
        """扫候选方向,选最朝目标且前方够空的方向;都不够空时返回最空的那个。

        候选按偏离目标方向 ``|offset|`` 从小到大试;先试哪一侧由 player_id 奇偶决定,
        用来打破两人同侧避让的对称。
        """
        sign_first = 1.0 if self.id % 2 == 0 else -1.0
        best_h = goal_dir
        best_clear = -math.inf

        offsets = [0.0]
        k = 1
        while k * PLAN_STEP <= PLAN_MAX_OFFSET + 1e-9:
            offsets.append(sign_first * k * PLAN_STEP)
            offsets.append(-sign_first * k * PLAN_STEP)
            k += 1

        for off in offsets:
            h = goal_dir + off
            clear = _heading_clearance(pose.x, pose.y, h, obstacles)
            if clear >= PLAN_CLEARANCE:
                return h
            if clear > best_clear:
                best_clear, best_h = clear, h
        return best_h

    # ------------------------------------------------------------------
    # 技术动作 1:五模式连续推球(核心进攻)
    # ------------------------------------------------------------------

    @staticmethod
    def _ball_local(ball, pose: Pose2D) -> tuple[float, float]:
        """球在本体系的位置 (前, 左)。"""
        dx = ball.x - pose.x
        dy = ball.y - pose.y
        c, s = math.cos(pose.theta), math.sin(pose.theta)
        return dx * c + dy * s, -dx * s + dy * c

    def push_ball(
        self, aim: tuple[float, float] | None = None,
    ) -> None:
        """五模式连续推球 —— 不射门,只用低速贴球把球顶向 ``aim`` 方向。

        ``aim`` 是给射门窗口用的自定义推球方向(问题 7)。默认瞄对方球门。
        其余逻辑不变:五个模式、迟滞、前馈、贴球降转。
        """
        ctx = self.context
        pose = self.pose
        ball = ctx.ball if ctx is not None else None
        if ctx is None or pose is None or ball is None:
            self.action = "push:stop"
            self.stop()
            return

        # 推球只走 set_velocity,必须先退出踢球技能(踢球独占底盘)
        self.release_kick()

        # ---------------- 推球目标 ----------------
        if aim is not None:
            gx, gy = aim
        else:
            gx, gy = ctx.field.length / 2.0, 0.0
        tgx, tgy = gx - ball.x, gy - ball.y
        dtg = math.hypot(tgx, tgy)
        if dtg > 0.01:
            ux, uy = tgx / dtg, tgy / dtg
        else:
            ux, uy = 1.0, 0.0
        px, py = -uy, ux                       # 垂直单位向量(指向球门线左侧)

        behind = (ball.x - pose.x) * ux + (ball.y - pose.y) * uy
        lat = (pose.x - ball.x) * px + (pose.y - ball.y) * py
        target_yaw = math.atan2(gy - pose.y, gx - pose.x)
        yerr = normalize_angle(target_yaw - pose.theta)
        bdist = math.hypot(ball.x - pose.x, ball.y - pose.y)

        bx, by = self._ball_local(ball, pose)
        bang = math.degrees(math.atan2(by, bx))

        # 边线排斥(世界系 y)
        half_w = ctx.field.width / 2.0
        d_right = pose.y + half_w
        d_left = half_w - pose.y
        risk = max(0.0, PUSH_SIDELINE_MARGIN_M - min(d_left, d_right))
        if d_left < d_right:
            rep_y = -risk
        elif d_right < d_left:
            rep_y = risk
        else:
            rep_y = 0.0
        world_rep = rep_y * PUSH_K_SIDELINE

        # 对手侧向排斥:对手挡在球门线附近时往另一侧让
        for r in ctx.opponents.values():
            if r.pose is None:
                continue
            odx, ody = r.pose.x - ball.x, r.pose.y - ball.y
            od = math.hypot(odx, ody)
            if od < 1e-6 or od > PUSH_OPP_REPEL_M:
                continue
            side = odx * px + ody * py          # >0: 对手在球门线左侧
            weight = (PUSH_OPP_REPEL_M - od) / PUSH_OPP_REPEL_M
            world_rep += (-1.0 if side > 0.0 else 1.0) * weight * PUSH_OPP_REPEL_GAIN

        # ---------------- 模式选择(带迟滞) ----------------
        prev = self._push_mode
        # 贴球模式的"退出"比"进入"更难(0.70 进 / 1.00 出),防止在边界每帧来回跳
        near_modes = ("TURN_ONLY", "ORBIT_BEHIND", "LATERAL_ALIGN", "ALIGN_PUSH")
        near_limit = (
            PUSH_NEAR_BALL_EXIT_M if prev in near_modes else PUSH_NEAR_BALL_M
        )
        if bdist >= near_limit:
            mode = "APPROACH"
        elif abs(bang) > PUSH_TURN_ONLY_ANGLE_DEG:
            mode = "TURN_ONLY"
        elif behind <= 0.0:
            mode = "ORBIT_BEHIND"
        elif prev == "ORBIT_BEHIND":
            mode = (
                "LATERAL_ALIGN" if abs(lat) < PUSH_LATERAL_ALIGNED_M
                else "ORBIT_BEHIND"
            )
        elif prev == "LATERAL_ALIGN":
            if abs(lat) < PUSH_LATERAL_ALIGNED_M:
                mode = "ALIGN_PUSH"
            elif abs(lat) >= PUSH_LATERAL_UPPER_M:
                mode = "ORBIT_BEHIND"
            else:
                mode = "LATERAL_ALIGN"
        else:
            if abs(lat) >= PUSH_LATERAL_UPPER_M:
                mode = "ORBIT_BEHIND"
            elif abs(lat) >= PUSH_LATERAL_ALIGNED_M:
                mode = "LATERAL_ALIGN"
            else:
                mode = "ALIGN_PUSH"
        self._push_mode = mode

        c, s = math.cos(pose.theta), math.sin(pose.theta)

        # ---------------- 分模式计算 ----------------
        if mode == "APPROACH":
            # 目标点:正常情况下是"球后 target_behind";但如果我在球**前方**
            # (behind<=0),直线奔向球会从错误一侧撞球(把球推向我方门),
            # 改为先去球侧方绕一下。**远近都要绕**,不只是贴球时——
            # 从 5m 外正面冲向球同样会把球顶回去。
            if behind <= 0.0:
                if lat > 0.05:
                    side = 1.0
                elif lat < -0.05:
                    side = -1.0
                else:
                    side = 1.0 if self.id % 2 == 0 else -1.0
                # 绕行点必须**真的在球后**(ball - u*back)。如果把 back 设成 0,
                # 绕行点就落在"球的正侧方",到那儿 behind 恰好等于 0,
                # 于是又满足 behind<=0 → 目标不变 → 误差归零 → 机器人原地冻结。
                tx = ball.x - ux * PUSH_DETOUR_BACK_M + px * side * PUSH_DETOUR_SIDE_M
                ty = ball.y - uy * PUSH_DETOUR_BACK_M + py * side * PUSH_DETOUR_SIDE_M
            else:
                # 目标点随距离平滑地从"球本身"过渡到"球后方 target_behind"
                if bdist < 1.0:
                    ratio = clamp(
                        (1.0 - bdist) / max(1.0 - PUSH_NEAR_BALL_M, 1e-6), 0.0, 1.0,
                    )
                else:
                    ratio = 0.0
                tx = ball.x - ux * (PUSH_TARGET_BEHIND_M * ratio)
                ty = ball.y - uy * (PUSH_TARGET_BEHIND_M * ratio)
            ddx, ddy = tx - pose.x, ty - pose.y
            body_x = ddx * c + ddy * s
            body_y = -ddx * s + ddy * c

            vx = clamp(
                PUSH_K_APPROACH * body_x,
                -PUSH_VX_MAX_REV, PUSH_VX_MAX_APPROACH,
            )
            vy = clamp(
                PUSH_K_APPROACH * body_y, -PUSH_VY_MAX, PUSH_VY_MAX,
            )
            # 排斥量(世界 y → 本体),离门越近越不排斥
            near_goal = clamp((dtg - 2.0) / 4.0, 0.0, 1.0)
            vy = clamp(
                vy + (world_rep * c) * near_goal, -PUSH_VY_MAX, PUSH_VY_MAX,
            )
            # 朝向:正常段朝球门(到位即对准);**绕行段朝绕行点**。
            # 绕行时目标在机器人身后,若仍朝球门,前进误差是负的,只能以
            # PUSH_VX_MAX_REV(0.2m/s)龟速倒车 —— 实测 4m 要爬 10 秒。
            if behind <= 0.0:
                face_yaw = math.atan2(ty - pose.y, tx - pose.x)
                w = clamp(
                    PUSH_K_YAW * normalize_angle(face_yaw - pose.theta),
                    -PUSH_W_MAX, PUSH_W_MAX,
                )
            else:
                w = clamp(PUSH_K_YAW * yerr, -PUSH_W_MAX, PUSH_W_MAX)
            vx = clamp(
                vx + (world_rep * s) * 0.2 * near_goal, -0.3, PUSH_VX_MAX_APPROACH,
            )
            vy = _soft_clip(vy, PUSH_SOFT_CLIP)
            w = _soft_clip(w, PUSH_SOFT_CLIP)

            # 接近保护:球太近就降速,防止撞飞球(只在近球区生效,
            # 远处全速接近 —— 否则远距离也一直被压到 0.35m/s,追不上球)
            if bdist < PUSH_GUARD_BALL_DIST_M:
                vx = clamp(vx, -PUSH_VX_MAX_REV, PUSH_GUARD_VX_MAX)
                vy = clamp(vy, -PUSH_GUARD_VY_MAX, PUSH_GUARD_VY_MAX)
                w = clamp(w, -PUSH_GUARD_W_MAX, PUSH_GUARD_W_MAX)

            debugdraw.point(tx, ty, rgb=(1.0, 0.5, 0.0), scale=0.14, ns="push_target")

        elif mode == "TURN_ONLY":
            # 球在侧面:原地转,禁止横移(横移会把球碰飞)
            vx = 0.0
            vy = 0.0
            w = clamp(
                PUSH_K_YAW * math.radians(bang), -PUSH_W_MAX, PUSH_W_MAX,
            )

        elif mode == "ORBIT_BEHIND":
            if behind <= 0.0:
                # 我在球**前方**且贴得很近：慢速横移绕后太慢(原地踏步横着走)。
                # 改用 walk_to 全速走到球后方绕行点，分两步:
                #   ① 确定"球后 + 侧向"的绕行点(和 APPROACH 绕行点一致)
                #   ② walk_to 带避球+避机器人走过去: 面向绕行点直走, 绕到球后
                #      再正常面朝对方球门推球。这一步速度 = MAX_LINEAR = 1.4 m/s,
                #      比 orbit 的 0.5~0.9 m/s 快 ~2~3 倍，也不再斜着横移。
                if lat > 0.05:
                    s_ = 1.0
                elif lat < -0.05:
                    s_ = -1.0
                else:
                    s_ = 1.0 if self.id % 2 == 0 else -1.0
                tx = ball.x - ux * PUSH_DETOUR_BACK_M + px * s_ * PUSH_DETOUR_SIDE_M
                ty = ball.y - uy * PUSH_DETOUR_BACK_M + py * s_ * PUSH_DETOUR_SIDE_M
                self.action = "push:go_behind"
                self.walk_to(
                    (tx, ty),
                    face=math.atan2(gy - pose.y, gx - pose.x),
                    avoid_ball=True, avoid_robots=True,
                )
                return
            else:
                depth_err = behind - PUSH_TARGET_BEHIND_M
                vx = clamp(
                    PUSH_K_DEPTH * depth_err,
                    -PUSH_VX_MAX_REV, PUSH_ORBIT_VX_MAX,
                )
                vy = clamp(-PUSH_K_LAT * lat, -PUSH_VY_MAX, PUSH_VY_MAX)
                w = clamp(PUSH_K_YAW * yerr, -PUSH_W_MAX, PUSH_W_MAX)
                vy = _soft_clip(vy, PUSH_SOFT_CLIP)
                w = _soft_clip(w, PUSH_SOFT_CLIP)

        elif mode == "LATERAL_ALIGN":
            # 已在球后,横向微调;前进放宽一点
            depth_err = behind - PUSH_TARGET_BEHIND_M
            vx = clamp(
                PUSH_K_DEPTH * depth_err,
                -PUSH_VX_MAX_REV, PUSH_LATERAL_VX_MAX,
            )
            vy = clamp(-PUSH_K_LAT * lat, -PUSH_VY_MAX, PUSH_VY_MAX)
            w = clamp(PUSH_K_YAW * yerr, -PUSH_W_MAX, PUSH_W_MAX)
            vy = _soft_clip(vy, PUSH_SOFT_CLIP)
            w = _soft_clip(w, PUSH_SOFT_CLIP)

        else:  # ALIGN_PUSH
            depth_err = behind - PUSH_TARGET_BEHIND_M
            ready = (
                behind > 0.0
                and abs(lat) < PUSH_LATERAL_ALIGNED_M
                and abs(math.degrees(yerr)) < PUSH_FACING_GOAL_DEG
                and bdist < PUSH_NEAR_BALL_M
            )
            vy = clamp(-PUSH_K_LAT * lat, -PUSH_VY_MAX, PUSH_VY_MAX)
            # 贴球降转:球越近,角速度越小(防转身撞飞球)
            damping = 0.3 + 0.7 * clamp((bdist - 0.3) / 0.5, 0.0, 1.0)
            w = clamp(PUSH_K_YAW * yerr * damping, -PUSH_W_MAX, PUSH_W_MAX)
            vy = _soft_clip(vy, PUSH_SOFT_CLIP)
            w = _soft_clip(w, PUSH_SOFT_CLIP)
            vx_upper = (
                PUSH_ALIGN_VX_MAX_READY if ready else PUSH_ALIGN_VX_MAX_UNREADY
            )
            # 前馈 + 深度 P:depth_err→0 时仍以 PUSH_SPEED_BASE 轻推
            vx = clamp(
                PUSH_SPEED_BASE + PUSH_K_DEPTH * depth_err,
                -PUSH_VX_MAX_REV, vx_upper,
            )

        self.action = f"push:{mode.lower()}"
        self.set_velocity(vx, vy, w)

    # ------------------------------------------------------------------
    # 技术动作 2:攻击(射门 or 推球)
    # ------------------------------------------------------------------

    def _lane_clearance(
        self, ctx: Context, ax: float, ay: float, bx: float, by: float,
    ) -> float:
        """线段 a→b 到最近对手的净空(已扣对手半径)。无对手返回 10。"""
        best = math.inf
        for r in ctx.opponents.values():
            if r.pose is None:
                continue
            d = _point_segment_distance(r.pose.x, r.pose.y, ax, ay, bx, by)
            best = min(best, d - OPPONENT_RADIUS)
        return 10.0 if best is math.inf else best

    def _shot_window(
        self, ctx: Context, ball,
    ) -> tuple[float, float] | None:
        """球周围找一个"直线能射进"的点。找不到返回 None。

        这就是用户说的"点到点坐标公式"：以球为圆心，按半径×角度撒候选点，
        逐个算"候选点 → 门框最佳落点"的净空，取净空够大、绕路又最短的。

        注意：这里算的是**球**该去哪，不是机器人该去哪。push_ball(aim=point)
        会直接把球顶向那个方向。
        """
        goal = opponent_goal(ctx)
        half_l = ctx.field.length / 2.0
        half_w = ctx.field.width / 2.0
        best = None
        best_score = -math.inf

        # 门框候选落点（内收一点，和 _best_goal_target 一致）
        goal_inset = min(SHOOT_AIM_INSET_M, ctx.field.goal_width / 4.0)
        goal_ys = (
            -ctx.field.goal_width / 2.0 + goal_inset,
            0.0,
            ctx.field.goal_width / 2.0 - goal_inset,
        )

        for deg in range(0, 360, SHOT_WINDOW_STEP_DEG):
            a = math.radians(deg)
            for r in SHOT_WINDOW_RADII:
                px = ball.x + r * math.cos(a)
                py = ball.y + r * math.sin(a)
                # 候选点本身必须在场内
                if not in_field(ctx, px, py, margin=SHOT_WINDOW_FIELD_MARGIN_M):
                    continue
                # 候选点距门太远就没意义
                if dist(px, py, goal[0], goal[1]) > SHOT_WINDOW_MAX_DIST_M:
                    continue
                # 候选点看球门的张角
                dx = goal[0] - px
                if dx <= 1e-6:
                    continue
                a1 = math.atan2(
                    ctx.field.goal_width / 2.0 - py, dx,
                )
                a2 = math.atan2(
                    -ctx.field.goal_width / 2.0 - py, dx,
                )
                aperture = abs(math.degrees(a1 - a2))
                if aperture < SHOT_WINDOW_MIN_APERTURE_DEG:
                    continue
                # 逐个门框落点算净空
                for gy in goal_ys:
                    clear = self._lane_clearance(ctx, px, py, goal[0], gy)
                    if clear < SHOT_WINDOW_CLEAR_M:
                        continue
                    # 评分：净空优先，其次绕路短，其次离门近
                    score = (
                        clear * 2.0
                        - 0.3 * r
                        - 0.05 * dist(px, py, goal[0], gy)
                    )
                    if score > best_score:
                        best_score = score
                        best = (px, py)
        return best
        gx = ctx.field.length / 2.0 + GOAL_TARGET_DEPTH_M
        half = ctx.field.goal_width / 2.0
        inset = min(SHOOT_AIM_INSET_M, half * 0.5)
        candidates = (-half + inset, 0.0, half - inset)

        best_y, best_score = 0.0, -math.inf
        for gy in candidates:
            clear = self._lane_clearance(ctx, ball.x, ball.y, gx, gy)
            gk_gap = min(
                [
                    dist(r.pose.x, r.pose.y, gx, gy)
                    for r in ctx.opponents.values() if r.pose is not None
                ] or [10.0]
            )
            score = min(clear, 1.0) + 0.35 * min(gk_gap, 2.0)
            if score > best_score:
                best_score, best_y = score, gy
        return (gx, best_y)

    # ------------------------------------------------------------------
    # 射门 / 传球 / 解围 的公共判据
    # ------------------------------------------------------------------

    def _ball_kickable(self, ctx: Context, ball) -> bool:
        """球是否在"踢得到"的位置:距离够近、在体坐标前方锥角内。

        ``kick()`` 会把球的体坐标 (ball_x_body, ball_y_body) 传给后端,球在身后
        或侧后方时踢出去会偏、甚至踢空。解围/传球/射门都必须先过这一关。
        """
        pose = self.pose
        if pose is None:
            return False
        d = dist(pose.x, pose.y, ball.x, ball.y)
        limit = SHOOT_TRIGGER_EXIT_M if self._kicking else SHOOT_TRIGGER_DIST_M
        if d > limit:
            return False
        bx, by = self._ball_local(ball, pose)
        if bx <= 0.0:
            return False
        return abs(math.degrees(math.atan2(by, bx))) <= SHOOT_MAX_BALL_ANGLE_DEG

    def _shoot_range(self, ctx: Context) -> float:
        """射门距离门槛。搏命放宽、死守收紧。"""
        mode = match_mode(ctx)
        if mode == "allin":
            return ALLIN_SHOOT_RANGE_M
        if mode == "park":
            return PARK_SHOOT_RANGE_M
        return SHOOT_RANGE_M

    def _under_pressure(self, ctx: Context, ball) -> bool:
        """是否有对手贴身(距球 < PRESSURE_DIST_M)。"""
        return any(
            r.pose is not None
            and dist(r.pose.x, r.pose.y, ball.x, ball.y) < PRESSURE_DIST_M
            for r in ctx.opponents.values()
        )

    def _should_clear(self, ctx: Context, ball) -> bool:
        """本方危险区 + 被压迫 → 大脚解围(不在后场组织)。

        推球只有 ~0.4m/s,在自家禁区被贴身时必然被断;一脚踢到对方半场边路
        至少能把战线推出去。死守模式(park)放宽到"过中线就解围"。
        """
        mode = match_mode(ctx)
        if mode == "park":
            line = 0.0                     # 死守:球过了中线就大脚开出去
        elif mode in ("allin", "press"):
            line = OWN_DANGER_X - 1.0      # 搏命:更靠里才解围,尽量组织进攻
        else:
            line = OWN_DANGER_X
        if ball.x > line:
            return False
        if mode != "park" and not self._under_pressure(ctx, ball):
            return False
        return self._ball_kickable(ctx, ball)

    def _clearance_target(self, ctx: Context, ball) -> tuple[float, float]:
        """解围落点：对方半场**中路**，扫描 y 候选选最空的。

        实测问题：原来固定瞄"对方半场边路"(y=±3.5)，球速 7m/s 时方向误差 4°
        就滚出边线 → R-01 判给对方界外球。一次解围白送一个定位球。

        新逻辑：固定 x = half_l - CLEAR_AIM_X_BACK_M，在 y 候选里扫描，选
        "球到该落点"这条直线的净空最大的。往对方球门方向踢（哪怕被断）也远
        好于踢出界。
        """
        f = ctx.field
        half_l = f.length / 2.0
        tx = clamp(
            half_l - CLEAR_AIM_X_BACK_M,
            -half_l + LEGAL_MARGIN_M, half_l - LEGAL_MARGIN_M,
        )
        best_y, best_clear = 0.0, -math.inf
        for ty in CLEAR_AIM_Y_CANDIDATES:
            clear = self._lane_clearance(ctx, ball.x, ball.y, tx, ty)
            if clear > best_clear:
                best_clear, best_y = clear, ty
        return (tx, best_y)

    def _best_goal_target(
        self, ctx: Context, ball,
    ) -> tuple[float, float]:
        """在门框内挑一个射门落点：优先"线净空 + 离门将远"。

        远射时瞄点往中间收：原来固定内收 0.35m，5m 外一脚偏 4° 就打在门柱
        外 → R-02 判给对方球门球。现在每超出一米中距多收 0.22m（上限 0.62m）。
        """
        gx = ctx.field.length / 2.0 + GOAL_TARGET_DEPTH_M
        half = ctx.field.goal_width / 2.0
        shot_d = dist(ball.x, ball.y, gx, 0.0)
        extra = clamp(
            SHOOT_AIM_FAR_EXTRA_M * max(0.0, shot_d - SHOOT_MID_M),
            0.0, SHOOT_AIM_FAR_MAX_M,
        )
        inset = min(SHOOT_AIM_INSET_M + extra, half * 0.5)
        candidates = (-half + inset, 0.0, half - inset)

        best_y, best_score = 0.0, -math.inf
        for gy in candidates:
            clear = self._lane_clearance(ctx, ball.x, ball.y, gx, gy)
            gk_gap = min(
                [
                    dist(r.pose.x, r.pose.y, gx, gy)
                    for r in ctx.opponents.values() if r.pose is not None
                ] or [10.0]
            )
            score = min(clear, 1.0) + 0.35 * min(gk_gap, 2.0)
            if score > best_score:
                best_score, best_y = score, gy
        return (gx, best_y)

    def _best_pass_target(
        self, ctx: Context, ball,
    ) -> tuple[float, float] | None:
        """找"明显更接近对方门"的队友,返回带提前量的接球点;没有则 None。"""
        goal = opponent_goal(ctx)
        my_d = dist(ball.x, ball.y, goal[0], goal[1])
        half_l = ctx.field.length / 2.0 - LEGAL_MARGIN_M
        half_w = ctx.field.width / 2.0 - LEGAL_MARGIN_M
        best: tuple[float, float] | None = None
        best_gain = PASS_MIN_GAIN_M
        for pid, r in ctx.teammates.items():
            if pid == self.id or r.pose is None:
                continue
            gain = my_d - dist(r.pose.x, r.pose.y, goal[0], goal[1])
            if gain <= best_gain:
                continue
            a = angle_to(r.pose.x, r.pose.y, goal[0], goal[1])
            # 提前量必须夹进场内:队友已经贴近底线时,"身前 0.6m" 会落到界外
            best = (
                clamp(r.pose.x + PASS_LEAD_M * math.cos(a), -half_l, half_l),
                clamp(r.pose.y + PASS_LEAD_M * math.sin(a), -half_w, half_w),
            )
            best_gain = gain
        return best

    def _can_pass(self, ctx: Context, ball) -> bool:
        """传球前置条件:能踢到 + 不在冷却期。"""
        if self._pass_cooldown_until is not None:
            if ctx.now < self._pass_cooldown_until:
                return False
            self._pass_cooldown_until = None
        return self._ball_kickable(ctx, ball)

    def _should_shoot(
        self, ctx: Context, ball, target: tuple[float, float] | None = None,
    ) -> bool:
        """是否该从"推球"切换到"踢球技能"。

        提交窗口(踢出后 0.55s 内不打断动作)统一由 ``attack()`` 顶部处理,
        因为解围/传球/射门共用同一个窗口。
        """
        pose = self.pose
        if pose is None:
            return False
        d = dist(pose.x, pose.y, ball.x, ball.y)

        limit = SHOOT_TRIGGER_EXIT_M if self._kicking else SHOOT_TRIGGER_DIST_M
        if d > limit:
            return False

        bx, by = self._ball_local(ball, pose)
        if bx <= 0.0:                       # 球在身后/侧面,踢不到
            return False
        if abs(math.degrees(math.atan2(by, bx))) > SHOOT_MAX_BALL_ANGLE_DEG:
            return False

        gx, gy = opponent_goal(ctx)
        shot_d = dist(ball.x, ball.y, gx, gy)
        if shot_d > self._shoot_range(ctx):
            return False

        # 射门线路被对手堵死 → 别盲射(白送球权),继续用推球绕开。
        # 注意判断的是"到**具体射门点**"的净空,不是到球门中心 —— 用球门中心
        # 判断的话,对方门将永远站在那条线上,中场就永远不敢射门了。
        # 距离用的是**球到门**的距离(不是人到球的距离)。
        # 近距离(<SHOOT_NEAR_M)例外:门前混战盲射也有机会,推球反而容易被抢。
        if shot_d > SHOOT_NEAR_M and target is not None:
            if self._lane_clearance(
                ctx, ball.x, ball.y, target[0], target[1],
            ) < SHOOT_LANE_CLEAR_M:
                return False
        return True

    def _shoot_power(self, ctx: Context, ball, target) -> float:
        d = dist(ball.x, ball.y, target[0], target[1])
        if d < SHOOT_NEAR_M:
            return SHOOT_POWER_NEAR
        if d < SHOOT_MID_M:
            return SHOOT_POWER_MID
        return SHOOT_POWER_FAR

    def attack(self, kick_target: tuple[float, float] | None = None) -> None:
        """攻击:四级决策树 —— 解围 > 射门 > 传球 > 推球。

        为什么是这个顺序:
          1. 球在自家危险区且被贴身 → 先保命,大脚踢出去。推球只有 0.4m/s,
             在这里必然被断,丢球就是空门。
          2. 有射门角度且线路没被封 → 射。踢球比推球快得多,门前一次触球
             就能解决问题。
          3. 射不了但队友位置明显更好 → 传。只有"射"和"推"两条路的话,
             对方只要堵住门前就守住了。
          4. 都不满足 → 五模式推球,用身体把球带向前场。

        踢球技能独占底盘,和推球(set_velocity)互斥;所以每一级都 return,
        绝不同时下发两种指令。
        """
        ctx = self.context
        ball = ctx.ball if ctx is not None else None
        if ctx is None or ball is None or self.pose is None:
            self.action = "attack:stop"
            self.stop()
            return

        # 踢球动作还没做完 → 什么都不做。解围/传球/射门共用同一个提交窗口，
        # 否则球被踢飞瞬间距离超阈值,push_ball 会把动作中途 release 掉 = 白踢一脚。
        if self._kick_commit_until is not None:
            if ctx.now < self._kick_commit_until:
                return
            self._kick_commit_until = None

        # ①' 抢球决断（问题 2/3）：被贴身、我方没控球、且球在本方半场/中场
        # → 最近者果断出脚，而不是慢速绕后被人断。we_have_ball 为 False 说明
        # 对手离球更近(或一样近)，这种 50/50 球拖下去必丢，直接处理掉最稳：
        #   - 线路空 → 射门(夺回主动权)；
        #   - 否则 → 解围/捅向对方半场中路，先夺回球权。
        # 仅当球不可踢(在脚侧/身后)才退回推球绕后——那时绕球已加速(见 param)。
        # 进攻三区(x ≥ CONTEST_CLEAR_X)被贴身则不过这里，落到下方正常推球护球，
        # 保留进攻机会。
        if ((not we_have_ball(ctx)) and self._under_pressure(ctx, ball)
                and ball.x < CONTEST_CLEAR_X):
            if self._ball_kickable(ctx, ball):
                kt = (kick_target if kick_target is not None
                      else self._best_goal_target(ctx, ball))
                if self._should_shoot(ctx, ball, kt):
                    self.action = "attack:shoot"
                    self._draw_kick_target(kt)
                    self.kick_toward(
                        kt, self._shoot_power(ctx, ball, kt), allow_goal=True,
                    )
                    return
                target = self._clearance_target(ctx, ball)
                self.action = "attack:clear"
                self._draw_kick_target(target)
                self.kick_toward(target, CLEAR_POWER)
                return

        # ① 解围
        if self._should_clear(ctx, ball):
            target = self._clearance_target(ctx, ball)
            self.action = "attack:clear"
            self._draw_kick_target(target)
            self.kick_toward(target, CLEAR_POWER)
            return

        # ② 射门
        if kick_target is None:
            kick_target = self._best_goal_target(ctx, ball)
        if self._should_shoot(ctx, ball, kick_target):
            self.action = "attack:shoot"
            self._draw_kick_target(kick_target)
            self.kick_toward(
                kick_target, self._shoot_power(ctx, ball, kick_target),
                allow_goal=True,
            )
            return

        # ③ 传球(射门被封 / 角度不够)
        pass_target = self._best_pass_target(ctx, ball)
        if pass_target is not None and self._can_pass(ctx, ball):
            if self._lane_clearance(
                ctx, ball.x, ball.y, pass_target[0], pass_target[1],
            ) >= PASS_LANE_CLEAR_M:
                d_pass = dist(ball.x, ball.y, pass_target[0], pass_target[1])
                self.action = "attack:pass"
                self._draw_kick_target(pass_target)
                self.kick_toward(
                    pass_target,
                    clamp(
                        PASS_POWER_BASE + PASS_POWER_PER_M * d_pass,
                        PASS_POWER_MIN, PASS_POWER_MAX,
                    ),
                )
                self._pass_cooldown_until = ctx.now + PASS_COOLDOWN_S
                return

        # ④ 推球：直线射不进 → 往射门窗口带球
        window = self._shot_window(ctx, ball)
        if window is not None:
            self.push_ball(aim=window)
            return
        self.push_ball()

    # ------------------------------------------------------------------
    # 技术动作 3:门将
    # ------------------------------------------------------------------

    def _ball_velocity(self, ctx: Context, ball) -> tuple[float, float]:
        """用相邻两帧差分估计球速(门将预判用)。跳变过大视为噪声。"""
        prev = self._ball_prev
        vx = vy = 0.0
        if prev is not None and ctx.dt > 1e-6:
            vx = (ball.x - prev[0]) / ctx.dt
            vy = (ball.y - prev[1]) / ctx.dt
            if math.hypot(vx, vy) > 12.0:
                vx = vy = 0.0
        self._ball_prev = (ball.x, ball.y)
        return vx, vy

    def guard(self, *, allow_rush: bool = True, on_line: bool = False) -> None:
        """守门:门线前滑动封角 + 危险时出击。

        站位不是"站死小禁区中央",而是:
        - x 固定在门线前 ``GUARD_LINE_OFFSET_M``(默认 0.6m)
        - y 跟随球 y 的 ``GUARD_Y_GAIN`` 比例(默认 0.6,不全跟以防被假动作晃开)
        - y 夹在 ``GUARD_Y_LIMIT_M``(门半宽 1.3,留 0.3 余量)
        - 球朝我方飞来时,按球速提前 ``GUARD_LEAD_S`` 秒预判

        出击条件:球进我方禁区,且我比队友更近(带 ``GUARD_RUSH_MARGIN_M`` 余量)。

        ``on_line=True``:改站**球门线上**(贴线 0.05m)。对方定位球期间必须这样——
        规则 R-07 的"门线防守"豁免要求站在本方球门线上,站门线前 0.6m 不算,
        会被判避让违例罚下 30s。

        ``allow_rush=False``:禁止出击。对方定位球期间必须关掉,否则门将冲出去
        碰球 = R-06 防守方先触球,罚下 30s + 重踢。
        """
        ctx = self.context
        pose = self.pose
        if ctx is None or pose is None:
            self.action = "guard:stop"
            self.stop()
            return

        f = ctx.field
        half_l = f.length / 2.0
        half_w = f.width / 2.0
        if on_line:
            home_x = -half_l + GK_LINE_X_MARGIN_M
            y_limit = GK_LINE_Y_LIMIT_M
        else:
            home_x = -half_l + GUARD_LINE_OFFSET_M
            y_limit = GUARD_Y_LIMIT_M

        ball = ctx.ball
        if ball is None:
            self.action = "guard:home"
            self.walk_to((home_x, 0.0), face=0.0, avoid_robots=True)
            return

        # --- 出击判定:球在我方禁区,且我比队友更近 ---
        in_box = (
            ball.x < -half_l + f.penalty_area_length
            and abs(ball.y) < f.penalty_area_width / 2.0
        )
        if allow_rush and in_box:
            mine = dist(pose.x, pose.y, ball.x, ball.y)
            others = [
                dist(r.pose.x, r.pose.y, ball.x, ball.y)
                for tid, r in ctx.teammates.items()
                if tid != self.id and r.pose is not None
            ]
            nearest_other = min(others) if others else math.inf
            if mine < nearest_other + GUARD_RUSH_MARGIN_M:
                self.action = "guard:rush"
                self.attack()
                return

        # --- 门线滑动 ---
        vx, vy = self._ball_velocity(ctx, ball)
        ball_y = ball.y
        if vx < -0.3:                      # 球正朝我方门飞
            # 预判"到门线那一刻的 y"：直接封那个点，比只跟当前球 y 更准。
            # t = (门线 x - 球 x) / vx；限到 GUARD_LEAD_MAX_S，防止过外推。
            t = (home_x - ball.x) / vx
            if t > 0.0:
                t = min(t, GUARD_LEAD_MAX_S)
                ball_y = ball.y + vy * t
        ball_y = clamp(ball_y, -half_w, half_w)   # 保险：别超出场地

        ty = clamp(ball_y * GUARD_Y_GAIN, -y_limit, y_limit)
        face = (
            angle_to(pose.x, pose.y, ball.x, ball.y)
            if GUARD_FACE_BALL else 0.0
        )
        self.action = "guard:line" if on_line else "guard:home"
        if on_line:
            # 贴门线必须**绕过 A***:全局规划器有 GLOBAL_FIELD_MARGIN_M=0.25 的
            # 边界余量,会把 (-6.95, y) 夹到 -6.75,站不到线上就丢掉 R-07 豁免。
            # 这里直接朝门线走(距离很短,不会碰到球),并把到达阈值收紧到 5cm,
            # 否则默认 15cm 的 ARRIVE_DIST 会让门将停在离门线 0.15m 的地方。
            self.walk_to(
                (home_x, ty), face=face,
                avoid_ball=False, avoid_robots=False, arrive_dist=0.05,
            )
            return
        # 平时守门:避球(别把球撞进自家门)+ 避人
        self.walk_to(
            (home_x, ty), face=face, avoid_ball=True, avoid_robots=True,
        )

    # ------------------------------------------------------------------
    # 技术动作 4:支援 / 防守
    # ------------------------------------------------------------------

    def support(self) -> None:
        """支援:按球权 + 比赛态势切换"前插接应"或"回撤补防"。

        我方控球 → 前插:跑到球前方 ``SUPPORT_ATTACK_AHEAD_M``、横向拉开
                   ``SUPPORT_ATTACK_SIDE_M`` 的接应点(球太靠后则不前压)。
        对方控球 → 回撤:站在 球→己方门 连线上距球 ``SUPPORT_DIST_M`` 处封堵。

        比赛态势(``match_mode``)会改写门槛,而不是新增一套流程:
          ``park``   领先/死守 → 不论球权一律退到本方补防位,宁可不进也不丢
          ``allin``  终场落后 → 我方控球或球已过中线就压到对方禁区边缘
          ``press``  落后     → 前插门槛降到 ``ATTACK_MIN_X_BEHIND``(更敢压上)
          ``hold`` / ``steady`` → 门槛提到 ``ATTACK_MIN_X_AHEAD``(收缩)
        """
        ctx = self.context
        pose = self.pose
        ball = ctx.ball if ctx is not None else None
        if ctx is None or pose is None or ball is None:
            self.action = "support:stop"
            self.stop()
            return

        f = ctx.field
        half_l = f.length / 2.0
        half_w = f.width / 2.0
        mode = match_mode(ctx)

        # --- 死守:不论球权,一律退到本方补防位 ---
        if mode == "park":
            tx = clamp(
                -half_l + PARK_SUPPORT_X_BACK_M,
                -half_l + LEGAL_MARGIN_M, half_l - LEGAL_MARGIN_M,
            )
            ty = clamp(ball.y * 0.6, -half_w + 0.5, half_w - 0.5)
            self.action = "support:park"
            self.move_to_position((tx, ty))
            return

        # --- 搏命:我方控球或球已过中线 → 压到对方禁区边缘抢二点 ---
        if mode == "allin" and (we_have_ball(ctx) or ball.x > 0.0):
            tx = clamp(
                half_l - ALLIN_SUPPORT_X_BACK_M,
                -half_l + LEGAL_MARGIN_M, half_l - LEGAL_MARGIN_M,
            )
            ty = clamp(
                -1.5 if ball.y > 0.0 else 1.5, -half_w + 0.5, half_w - 0.5,
            )
            self.action = "support:allin"
            self.move_to_position((tx, ty))
            return

        if mode == "press":
            min_x = ATTACK_MIN_X_BEHIND
        elif mode in ("hold", "steady"):
            min_x = ATTACK_MIN_X_AHEAD
        else:
            min_x = SUPPORT_ATTACK_MIN_X

        if we_have_ball(ctx) and ball.x > min_x:
            # --- 前插接应 ---
            # 横向往场地中路拉开(而不是固定 y=±1.5):球贴边线时固定 y 会让
            # 接应点反而跑到球的内侧,失去接应角度。
            side = -1.0 if ball.y > 0.0 else 1.0
            tx = clamp(
                ball.x + SUPPORT_ATTACK_AHEAD_M, -half_l + 0.8, half_l - 0.8,
            )
            ty = clamp(
                ball.y + side * SUPPORT_ATTACK_SIDE_M, -half_w + 0.5, half_w - 0.5,
            )
            self.action = "support:push_up"
            self.move_to_position((tx, ty))
            return

        # --- 回撤补防:球→己方门连线上,距球 standoff ---
        gx, gy = own_goal(ctx)
        dx, dy = gx - ball.x, gy - ball.y
        d = math.hypot(dx, dy)
        if d < 1e-6:
            ux, uy = -1.0, 0.0
        else:
            ux, uy = dx / d, dy / d

        # 关键：对方控球时**必须持续逼近**，不能站在补防点上不动。
        # 否则 walk_to 立刻"已到达" → 停车 → 原地踏步 → M-05 罚下 30s。
        standoff = SUPPORT_DIST_M
        cur_d = dist(pose.x, pose.y, ball.x, ball.y)
        if not we_have_ball(ctx) and cur_d <= standoff + ARRIVE_DIST + 0.05:
            # 已经站到位 → 再往前压一点，保证一直在动
            standoff = max(
                PRESS_CREEP_MIN_STANDOFF_M,
                cur_d - PRESS_CREEP_STEP_M,
            )
            # 压到 PRESS_ENGAGE_M 以内就真抢
            if cur_d <= PRESS_ENGAGE_M:
                self.action = "support:press"
                self.attack()
                return

        along = min(standoff, d)
        tx = ball.x + ux * along
        ty = ball.y + uy * along
        tx = clamp(tx, -half_l + LEGAL_MARGIN_M, half_l)
        ty = clamp(ty, -half_w + 0.3, half_w - 0.3)
        self.action = "support:drop"
        self.move_to_position((tx, ty))

    def defend(self) -> None:
        """防守滑步:锚定本方半场,横向跟球;球太近则后退。"""
        ctx = self.context
        pose = self.pose
        ball = ctx.ball if ctx is not None else None
        if ctx is None or pose is None:
            self.action = "defend:stop"
            self.stop()
            return

        if ball is not None and dist(pose.x, pose.y, ball.x, ball.y) < DEFEND_BACKOFF_M:
            self.action = "defend:backoff"
            self.release_kick()
            self.set_velocity(-DEFEND_BACKOFF_VEL, 0.0, 0.0)
            return

        ball_y = 0.0 if ball is None else ball.y
        ty = clamp(ball_y, -DEFEND_Y_LIMIT_M, DEFEND_Y_LIMIT_M)
        self.action = "defend:slide"
        self.move_to_position((DEFEND_ANCHOR_X_M, ty))

    # ------------------------------------------------------------------
    # 技术动作 5:绕后踢球 / 定位球避让
    # ------------------------------------------------------------------

    def stage_behind_and_kick(
        self,
        target: tuple[float, float],
        power: float,
        *,
        stage_m: float = KICKOFF_STAGE_M,
        lateral_tol: float = KICKOFF_LATERAL_TOL,
        allow_goal: bool = False,
    ) -> bool:
        """绕到"球→target"连线后方并对准,到位就把球踢向 target。

        返回本帧是否已踢出。未就位时只走位(避球,绝不碰球)。
        开球、定位球、传球都走这一个入口。

        ``lateral_tol`` 是横向对准容差。走位用 0.35(宽松,快);定位球射门时
        必须收紧到 0.20 并把 ``stage_m`` 拉长到 2.0 —— 在 1.2m 处 0.35m 的横向
        偏差等于 16°,射门会直接打飞;在 2.0m 处 0.20m 只有 5.7°。

        **兜底**:角球、贴边线的界外球,球的"后方"在场地外,机器人永远站不到
        `behind <= KICKOFF_FRONT_MARGIN` 的位置。没有兜底就会原地打转直到
        定位球 45s 超时、球权开放。这里加计时:超时且离球够近就直接触球,
        把死球解开(R-05:主罚方触球即 ball in play),剩下的交给正常拼抢。
        """
        ctx = self.context
        pose = self.pose
        ball = ctx.ball if ctx is not None else None
        if ctx is None or pose is None or ball is None:
            self.stop()
            return False

        # 目标变了就重置计时(不同定位球之间不能串)
        if (
            self._stage_target is None
            or dist(target[0], target[1], self._stage_target[0], self._stage_target[1])
            > 1.0
        ):
            self._stage_target = target
            self._stage_start = ctx.now
            self._stage_leg = 0

        kick_dir = angle_to(ball.x, ball.y, target[0], target[1])
        cos_k, sin_k = math.cos(kick_dir), math.sin(kick_dir)
        rx, ry = pose.x - ball.x, pose.y - ball.y
        behind = rx * cos_k + ry * sin_k            # <0 表示在球后(正确侧)
        lateral = abs(-rx * sin_k + ry * cos_k)

        if behind > KICKOFF_FRONT_MARGIN or lateral > lateral_tol:
            half_l = ctx.field.length / 2.0 - LEGAL_MARGIN_M
            half_w = ctx.field.width / 2.0 - LEGAL_MARGIN_M
            ideal = (ball.x - cos_k * stage_m, ball.y - sin_k * stage_m)
            outside = abs(ideal[0]) > half_l or abs(ideal[1]) > half_w

            if outside:
                # 角球 / 贴边线定位球:球的"后方"在场地外。从场内**没法**把球往
                # 场内踢(球在机器人和落点之间),必须站到场外去。
                # 规则只有 `set` 阶段"场外"才罚 P-01,定位球是在 `playing` 阶段
                # 执行的,出界不违规。
                sp = (
                    clamp(ideal[0], -half_l - STAGE_OUTSIDE_M, half_l + STAGE_OUTSIDE_M),
                    clamp(ideal[1], -half_w - STAGE_OUTSIDE_M, half_w + STAGE_OUTSIDE_M),
                )
                sx = 1.0 if ball.x >= 0.0 else -1.0
                sy = 1.0 if ball.y >= 0.0 else -1.0
                # 过渡点:**已经站在场外**(边线外 0.8m),且沿边线离球 1.6m。
                # 先横移出场、再从场外绕过球,才不会一路把球撞出界。
                # 0.8/1.6 这两个数不能再小:第二段是斜线,机器人的转弯弧会往里
                # 偏 ~0.3m,余量不够就会蹭到球、把球顶出场外。
                if abs(sp[1]) > half_w:
                    wp = (
                        ball.x - STAGE_DETOUR_SIDE_M * sx,
                        sy * (half_w + STAGE_DETOUR_OUT_M),
                    )
                else:
                    wp = (
                        sx * (half_l + STAGE_DETOUR_OUT_M),
                        ball.y - STAGE_DETOUR_SIDE_M * sy,
                    )
                # 分两段走(用状态,不用每帧算净空 —— 后者在临界值上会逐帧翻转)
                if self._stage_leg == 0 and dist(
                    pose.x, pose.y, wp[0], wp[1],
                ) < STAGE_DETOUR_ARRIVE_M:
                    self._stage_leg = 1
                target = sp if self._stage_leg == 1 else wp
                self.release_kick()
                self.walk_to(
                    target, face=kick_dir, avoid_ball=False, avoid_robots=False,
                )
                return False

            # 场内:夹进场地 + 正常避障
            stage = (
                clamp(ideal[0], -half_l, half_l),
                clamp(ideal[1], -half_w, half_w),
            )
            # 夹回来之后如果几乎贴在球上,说明真正的"球后"在场外,不可能站到
            unreachable = dist(stage[0], stage[1], ball.x, ball.y) < STAGE_UNREACHABLE_M
            # 注意:不能用 `self._stage_start or ctx.now` —— 开赛第 0 秒 ctx.now 就是
            # 0.0,是假值,会让 elapsed 永远算成 0、兜底永不触发。
            started = ctx.now if self._stage_start is None else self._stage_start
            elapsed = ctx.now - started
            limit = (
                SETPLAY_UNREACHABLE_TIMEOUT_S if unreachable
                else SETPLAY_STAGE_TIMEOUT_S
            )
            if elapsed > limit and dist(
                pose.x, pose.y, ball.x, ball.y,
            ) < SETPLAY_FORCE_KICK_DIST_M:
                self.action = "stage:force_kick"
                self.kick_toward(target, power, allow_goal=allow_goal)
                return True
            self.release_kick()
            self.walk_to(
                stage, face=kick_dir, avoid_ball=True, avoid_robots=True,
            )
            return False

        # 落点过 legal_kick_target:非射门落点夹进场内(开球/定位球传球落点
        # 不会把球踢出界),射门落点(allow_goal=True)原样保留。开球不能直
        # 接得分(G-03),所以开球调用方传 allow_goal=False。
        self.kick_toward(target, power, allow_goal=allow_goal)
        return True

    def retreat_to(
        self,
        point: tuple[float, float] | None = None,
        min_dist: float = OPP_SET_AVOID_M,
    ) -> bool:
        """对方定位球避让:先保证离球 ≥ ``min_dist``,再走到指定站位。

        返回 True 表示已经"退够远"(可以安心站位)。
        规则 R-06/R-07:主罚方触球前防守方距球 < 1.45m 会被罚下 30s。
        这里用 1.65m + 0.25m 余量。
        """
        ctx = self.context
        pose = self.pose
        ball = ctx.ball if ctx is not None else None
        if ctx is None or pose is None:
            self.stop()
            return False
        if ball is None:
            if point is not None:
                self.move_to_position(point)
            else:
                self.stop()
            return True

        need = min_dist + OPP_SET_AVOID_EXTRA_M
        d = dist(pose.x, pose.y, ball.x, ball.y)
        if d < need:
            # 沿"球→我"方向径向退出去(径向速度 > 0.05 满足"正在远离"豁免)。
            # 注意目标点要**多退一步**:如果目标就落在脚下(<ARRIVE_DIST),
            # walk_to 会认为"已到达"直接停车,于是既不退也不上岗,卡在
            # 1.75~1.9m 的死区里。多退 RETREAT_EXTRA_STEP_M 保证每帧都真在动。
            dx, dy = pose.x - ball.x, pose.y - ball.y
            n = math.hypot(dx, dy)
            if n < 1e-6:
                dx, dy, n = -1.0, 0.0, 1.0
            far = need + RETREAT_EXTRA_STEP_M
            target = (ball.x + dx / n * far, ball.y + dy / n * far)
            self.action = "retreat:away"
            self.release_kick()
            self.walk_to(
                target,
                face=angle_to(pose.x, pose.y, ball.x, ball.y),
                avoid_ball=True,
                avoid_robots=True,
            )
            return False

        if point is None:
            self.action = "retreat:hold"
            self.stop()
            return True

        self.action = "retreat:hold"
        self.move_to_position(point)
        return True

    def take_kickoff(
        self,
        kick_target: tuple[float, float] | None = None,
        power: float = KICK_POWER_OUR_KICKOFF,
    ) -> None:
        """我方开球:走到球后并把球踢向 ``kick_target``。

        注意:开球**不能直接射门**(规则 G-03 需 ≥2 次触球才有效),
        所以默认目标是"往前推一点"的短传点,而不是球门。
        """
        ctx = self.context
        ball = ctx.ball if ctx is not None else None
        if ctx is None or ball is None:
            self.stop()
            return
        if kick_target is None:
            kick_target = (ball.x + 3.0, 0.0)
        self.action = "kickoff"
        self.stage_behind_and_kick(kick_target, power)

    # ------------------------------------------------------------------
    # 通用站位
    # ------------------------------------------------------------------

    def block_path_projection(
        self, opponent_id: int,
    ) -> tuple[float, float, float, float] | None:
        """自己到 对手→球 线段的垂足:返回 (x, y, 垂距, 线段参数 t)。"""
        ctx = self.context
        pose = self.pose
        ball = ctx.ball if ctx is not None else None
        opponent = ctx.opponents.get(opponent_id) if ctx is not None else None
        if ctx is None or pose is None or ball is None or opponent is None:
            return None
        if opponent.pose is None:
            return None

        ax, ay = opponent.pose.x, opponent.pose.y
        bx, by = ball.x, ball.y
        vx, vy = bx - ax, by - ay
        length2 = vx * vx + vy * vy
        if length2 < 1e-6:
            return None

        raw_t = ((pose.x - ax) * vx + (pose.y - ay) * vy) / length2
        t = clamp(raw_t, 0.0, 1.0)
        tx = ax + vx * t
        ty = ay + vy * t
        return tx, ty, dist(pose.x, pose.y, tx, ty), raw_t

    def move_to_position(self, target: tuple[float, float] | None) -> None:
        """走到站位点(支援/防守/避让),面向球,避球+避机器人。"""
        if target is None:
            self.stop()
            return
        face = None
        ball = self.context.ball if self.context is not None else None
        if ball is not None and self.pose is not None:
            face = angle_to(self.pose.x, self.pose.y, ball.x, ball.y)
        self.release_kick()
        self.walk_to(target, face=face, avoid_ball=True, avoid_robots=True)

    @staticmethod
    def _angular(err: float) -> float:
        return clamp(ANGULAR_GAIN * err, -MAX_ANGULAR, MAX_ANGULAR)
