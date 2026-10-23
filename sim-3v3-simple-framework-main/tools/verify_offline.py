"""3v3 策略离线验证:假后端 + 简化物理(含踢球),把每个 Phase 都跑一遍并检查不变量。

用法: python verify_bugs.py
不修改项目任何文件。
"""
from __future__ import annotations

import dataclasses
import math
import random
import sys
import types
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ---- stub 平台 SDK(main.py 模块级 import AgentBase) ----
_stub = types.ModuleType("booster_agent_framework")


class AgentBase:
    def __init__(self, *a, **k):
        pass


class AgentFeatures:
    pass


_stub.AgentBase = AgentBase
_stub.AgentFeatures = AgentFeatures
sys.modules["booster_agent_framework"] = _stub

from src.framework.types import (  # noqa: E402
    ADULT_FIELD_DIMENSIONS, BallState, Context, GameControlState, GameState,
    Pose2D, RobotState, SetPlay, TeamState, PlayerState, KICKING_TEAM_NONE,
)
from src.main import (  # noqa: E402
    SoccerSimAgent, Phase, get_phase, _set_play_aim, _set_play_support_point,
)
from src.player import Player  # noqa: E402
from src import param  # noqa: E402


F = ADULT_FIELD_DIMENSIONS
DT = 1.0 / 30.0
ISSUES: list[str] = []


def issue(tag: str, msg: str) -> None:
    ISSUES.append(f"[{tag}] {msg}")


def report(tag, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {tag}  {detail}")
    if not ok:
        issue(tag, detail)


# ======================================================================
# 假后端:记录指令,模拟"踢球独占底盘"
# ======================================================================
class FakeBackend:
    def __init__(self) -> None:
        self.mode = "walk"
        self.fall_down_state = "normal"
        self.vx = self.vy = self.w = 0.0
        self.kicking = False
        self.kick_calls = 0
        self.get_up_calls = 0
        self.mode_calls = 0            # request_mode 被真正下发的次数(防抖测试用)
        self.ignore_mode_request = False   # True = 模拟 SDK 卡在旧模式不切
        self.vel_history: list[tuple[float, float, float]] = []
        self.pending_kick: tuple[float, float] | None = None  # (方向体坐标, 力度)
        self.bad_values = 0

    def set_velocity(self, vx, vy, w):
        for v in (vx, vy, w):
            if not math.isfinite(v):
                self.bad_values += 1
                return
        if self.kicking:
            return
        self.vx, self.vy, self.w = vx, vy, w
        self.vel_history.append((vx, vy, w))

    def kick(self, d, p, bx, by):
        if not self.kicking:
            self.pending_kick = (d, p)     # 只在"刚开始踢"时算一次触球
        self.kick_calls += 1
        self.kicking = True

    def release_kick(self):
        self.kicking = False

    def request_mode(self, m):
        self.mode_calls += 1
        if self.ignore_mode_request:   # 模拟 SDK 迟迟不进入目标模式
            return
        self.mode = m

    def get_up(self):
        self.get_up_calls += 1


class World:
    def __init__(self, n_ours=3, n_opps=3):
        self.config = types.SimpleNamespace(
            team_id=1, player_ids=list(range(1, n_ours + 1)),
        )
        self.players = [
            Player(pid, self.config, FakeBackend()) for pid in range(1, n_ours + 1)
        ]
        self.poses = {p.id: [0.0, 0.0, 0.0] for p in self.players}
        self.opp_poses = {i: [0.0, 0.0, 0.0] for i in range(1, n_opps + 1)}
        self.ball = [0.0, 0.0]
        self.ball_vel = [0.0, 0.0]
        self.game = None
        self.no_ball = False
        self.penalized: set[int] = set()
        self.last_kick_dir: float | None = None
        self.t = 0.0
        self.store = types.SimpleNamespace()
        SoccerSimAgent.init_store(SoccerSimAgent, self.store)

    def set_ours(self, *specs):
        for pid, (x, y, th) in specs:
            self.poses[pid] = [x, y, th]

    def set_opps(self, *specs):
        for pid, (x, y, th) in specs:
            self.opp_poses[pid] = [x, y, th]

    def set_ball(self, x, y, vx=0.0, vy=0.0):
        self.ball = [x, y]
        self.ball_vel = [vx, vy]

    def make_game(self, state=GameState.PLAYING, set_play=SetPlay.NONE,
                  kicking_team=KICKING_TEAM_NONE, secondary=0, stopped=False,
                  score=(0, 0), secs=300):
        teams = (
            TeamState(team_number=1, score=score[0],
                      players=tuple(PlayerState() for _ in range(20))),
            TeamState(team_number=2, score=score[1],
                      players=tuple(PlayerState() for _ in range(20))),
        )
        self.game = GameControlState(
            state=state, set_play=set_play, kicking_team=kicking_team,
            secondary_time=secondary, stopped=stopped, teams=teams,
            secs_remaining=secs,
            last_seen_at=self.t,
        )

    def context(self):
        ball = None
        if not self.no_ball:
            ball = BallState(x=self.ball[0], y=self.ball[1], last_seen_at=self.t)
        return Context(
            now=self.t, dt=DT, team_id=1, field=F, game=self.game, ball=ball,
            teammates={pid: RobotState(pid, Pose2D(*p), self.t)
                       for pid, p in self.poses.items() if pid not in self.penalized},
            opponents={pid: RobotState(pid, Pose2D(*p), self.t)
                       for pid, p in self.opp_poses.items()},
        )

    def tick(self, n=1):
        for _ in range(n):
            ctx = self.context()
            for p in self.players:
                p.context = ctx
            SoccerSimAgent.play(ctx, self.players, self.store)
            self._integrate()
            self.t += DT

    def _integrate(self):
        kicked_this_tick = False
        for p in self.players:
            b = p._backend
            # 踢球:把球沿体坐标方向射出去(只有球在脚下才踢得中)
            if b.pending_kick is not None:
                d, power = b.pending_kick
                b.pending_kick = None
                x, y, th = self.poses[p.id]
                # 框架约定:踢球技能自己负责"走到球边抬脚",触发距离上限 2m
                if math.hypot(self.ball[0] - x, self.ball[1] - y) <= 2.0:
                    world_dir = th + d
                    speed = power * 0.8
                    self.ball_vel = [
                        math.cos(world_dir) * speed, math.sin(world_dir) * speed,
                    ]
                    self.last_kick_dir = world_dir
                    kicked_this_tick = True
            if b.kicking or p.id in self.penalized:
                continue
            x, y, th = self.poses[p.id]
            c, s = math.cos(th), math.sin(th)
            wx = b.vx * c - b.vy * s
            wy = b.vx * s + b.vy * c
            self.poses[p.id] = [x + wx * DT, y + wy * DT, th + b.w * DT]

        # 球被机器人身体推动(踢球的同一帧不算,脚比身体快)
        for p in self.players:
            if kicked_this_tick:
                break
            if p.id in self.penalized:
                continue
            b = p._backend
            if b.kicking:
                continue
            x, y, th = self.poses[p.id]
            dx, dy = self.ball[0] - x, self.ball[1] - y
            d = math.hypot(dx, dy)
            if d < 1e-6 or d > 0.62:
                continue
            c, s = math.cos(th), math.sin(th)
            wx = b.vx * c - b.vy * s
            wy = b.vx * s + b.vy * c
            ux, uy = dx / d, dy / d
            along = wx * ux + wy * uy
            if along > 0.0:
                self.ball_vel = [ux * along * 1.1, uy * along * 1.1]

        self.ball[0] += self.ball_vel[0] * DT
        self.ball[1] += self.ball_vel[1] * DT
        sp = math.hypot(*self.ball_vel)
        if sp > 1e-6:
            ns = max(0.0, sp - 1.2 * DT)
            self.ball_vel = [self.ball_vel[0] / sp * ns, self.ball_vel[1] / sp * ns]

    def dists_to_ball(self):
        return {p.id: math.hypot(self.poses[p.id][0] - self.ball[0],
                                 self.poses[p.id][1] - self.ball[1])
                for p in self.players}


# ======================================================================
# S1 NORMAL
# ======================================================================
def s1_normal():
    w = World()
    w.set_ours((1, (-2.0, 0.0, 0.0)), (2, (1.0, 2.0, 0.0)), (3, (-6.0, 0.0, 0.0)))
    w.set_opps((1, (3.0, 0.5, 3.14)), (2, (5.0, -2.0, 3.14)), (3, (6.0, 1.5, 3.14)))
    w.set_ball(0.5, 0.0)
    w.make_game()
    actions = {}
    for _ in range(600):
        w.tick(1)
        for p in w.players:
            actions.setdefault(p.id, {}).setdefault(p.action, 0)
            actions[p.id][p.action] += 1
    report("S1 NORMAL 20s 球进对方半场深处", w.ball[0] > 4.0,
           f"球={tuple(round(v,2) for v in w.ball)}")
    report("S1 门将锁定为离己方门最近者(id=3)", w.store.gk_id == 3,
           f"gk_id={w.store.gk_id}")
    bad = [p.id for p in w.players if p._backend.bad_values]
    report("S1 无 NaN/inf 指令", not bad, f"{bad}")
    print("     动作分布:", {k: dict(v) for k, v in actions.items()})


# ======================================================================
# S2 OPP_SET_PLAY:避让 1.65m + 绝不碰球 + 门将贴门线
# ======================================================================
def s2_opp_set_play():
    w = World()
    w.set_ours((1, (-3.0, 0.5, 0.0)), (2, (-4.5, 2.0, 0.0)), (3, (-6.5, 0.0, 0.0)))
    w.set_opps((1, (-3.6, 0.0, 0.0)), (2, (0.0, 3.0, 3.14)), (3, (2.0, -3.0, 3.14)))
    w.set_ball(-4.0, 0.0)
    w.make_game(set_play=SetPlay.DIRECT_FREE_KICK, kicking_team=2)
    report("S2 phase=OPP_SET_PLAY", get_phase(w.context()) == Phase.OPP_SET_PLAY)

    min_d, ball_moved = 99.0, 0.0
    b0 = tuple(w.ball)
    for i in range(300):
        w.tick(1)
        if i < 30:          # 前 1s 是"从违规位置退出去"的过程,不计入
            continue
        for pid, d in w.dists_to_ball().items():
            if pid == w.store.gk_id:
                continue
            min_d = min(min_d, d)
        ball_moved = max(ball_moved, math.hypot(w.ball[0] - b0[0], w.ball[1] - b0[1]))
    report("S2 1s 后非门将离球始终 ≥1.65m", min_d >= 1.65 - 1e-6, f"最近={min_d:.2f}m")
    report("S2 全程未碰球", ball_moved < 0.05, f"球位移={ball_moved:.3f}m")
    gk = w.store.gk_id
    gx, gy = w.poses[gk][0], w.poses[gk][1]
    report("S2 门将贴在球门线上(拿 R-07 豁免)", gx <= -6.9,
           f"门将=({gx:.2f},{gy:.2f}) 门线x=-7.00")
    report("S2 门将 y 在门宽内", abs(gy) <= F.goal_width / 2.0,
           f"|y|={abs(gy):.2f} 门半宽={F.goal_width/2}")
    print(f"     各人动作={[p.action for p in w.players]}")


# ======================================================================
# S3 OUR_SET_PLAY 角球(最容易卡死的场景)
# ======================================================================
def s3_our_corner():
    w = World()
    w.set_ours((1, (2.0, 0.0, 0.0)), (2, (3.0, 1.0, 0.0)), (3, (-6.0, 0.0, 0.0)))
    w.set_opps((1, (6.0, 1.0, 3.14)), (2, (5.0, -1.0, 3.14)), (3, (6.5, 0.0, 3.14)))
    w.set_ball(6.95, 4.45)      # R-04:角旗区向内 0.05m
    w.make_game(set_play=SetPlay.CORNER_KICK, kicking_team=1)
    report("S3 phase=OUR_SET_PLAY", get_phase(w.context()) == Phase.OUR_SET_PLAY)
    kicked_at = None
    first_dir = None
    taker_ids = set()
    for i in range(400):
        before = sum(p._backend.kick_calls for p in w.players)
        w.tick(1)
        if sum(p._backend.kick_calls for p in w.players) > before and kicked_at is None:
            kicked_at = i
            first_dir = w.last_kick_dir
            taker_ids.add(next(p.id for p in w.players if p._backend.kick_calls))
    report("S3 角球在时限内被踢出", kicked_at is not None,
           "第 %s 帧 (%s)" % (kicked_at,
                              "超时未踢" if kicked_at is None
                              else "%.1fs" % (kicked_at / 30.0)))
    report("S3 主罚者唯一", len(taker_ids) <= 1, f"{taker_ids}")
    moved = math.hypot(w.ball[0] - 6.95, w.ball[1] - 4.45)
    report("S3 球被踢离角旗区", moved > 0.5,
           f"位移={moved:.2f}m 球=({w.ball[0]:.2f},{w.ball[1]:.2f})")
    # 踢的方向要朝落点(5.5, 0.8),不能朝场外
    want = math.atan2(0.8 - 4.45, 5.5 - 6.95)
    err = None
    if first_dir is not None:
        err = abs(math.degrees(math.atan2(
            math.sin(first_dir - want), math.cos(first_dir - want))))
    report("S3 踢球方向朝落点(±25°)", err is not None and err < 25.0,
           f"偏差={None if err is None else round(err,1)}°")
    print(f"     球最终=({w.ball[0]:.2f},{w.ball[1]:.2f})  "
          f"动作={[p.action for p in w.players]}")


# ======================================================================
# S4 OUR_KICKOFF
# ======================================================================
def s4_our_kickoff():
    w = World()
    w.set_ours((1, (-1.3, 0.0, 0.0)), (2, (-1.0, 2.5, 0.0)), (3, (-6.5, 0.0, 0.0)))
    w.set_opps((1, (-2.0, 1.0, 0.0)), (2, (-2.0, -1.0, 0.0)), (3, (-4.0, 0.0, 0.0)))
    w.set_ball(0.0, 0.0)
    w.make_game(secondary=10, kicking_team=1)
    report("S4 phase=OUR_KICKOFF", get_phase(w.context()) == Phase.OUR_KICKOFF)
    kicked_at = None
    for i in range(120):
        before = sum(p._backend.kick_calls for p in w.players)
        w.tick(1)
        if sum(p._backend.kick_calls for p in w.players) > before and kicked_at is None:
            kicked_at = i
    report("S4 开球被踢出", kicked_at is not None, f"第 {kicked_at} 帧")
    report("S4 球离开中心点", math.hypot(*w.ball) > 0.15,
           f"球=({w.ball[0]:.2f},{w.ball[1]:.2f})")
    report("S4 球向前(+x)推进", w.ball[0] > 0.2, f"x={w.ball[0]:.2f}")


# ======================================================================
# S5 STOPPED / SET:必须零速
# ======================================================================
def s5_stopped():
    for state, tag, stopped in (
        (GameState.SET, "SET", False),
        (GameState.INITIAL, "INITIAL", False),
        (GameState.PLAYING, "PLAYING+stopped", True),
    ):
        w = World()
        w.set_ours((1, (-2.0, 1.0, 0.5)), (2, (1.0, 0.0, 0.0)), (3, (-6.0, 0.0, 0.0)))
        w.set_opps((1, (2.0, 0.0, 3.14)), (2, (3.0, 1.0, 3.14)), (3, (4.0, -1.0, 3.14)))
        w.set_ball(0.5, 0.5)
        w.make_game(state=state, stopped=stopped)
        report(f"S5 {tag} → phase=STOPPED", get_phase(w.context()) == Phase.STOPPED)
        p0 = {p.id: tuple(w.poses[p.id]) for p in w.players}
        w.tick(60)
        moved = max(math.hypot(w.poses[p.id][0] - p0[p.id][0],
                               w.poses[p.id][1] - p0[p.id][1]) for p in w.players)
        report(f"S5 {tag} 2s 内零位移", moved < 1e-9, f"最大位移={moved:.6f}m")


# ======================================================================
# S6 OPP_KICKOFF
# ======================================================================
def s6_opp_kickoff():
    # 真实场景:READY 阶段已经把所有人摆到"本方半场 + 中圈外",OPP_KICKOFF
    # 只需要微调。所以起点用合法位置(非法起点单独在 S6b 里压测)。
    w = World()
    w.set_ours((1, (-2.2, 1.5, 0.0)), (2, (-2.2, -1.5, 0.0)), (3, (-6.0, 0.0, 0.0)))
    w.set_opps((1, (1.0, 0.0, 3.14)), (2, (2.0, 1.0, 3.14)), (3, (3.0, -1.0, 3.14)))
    w.set_ball(0.0, 0.0)
    w.make_game(secondary=10, kicking_team=2)
    report("S6 phase=OPP_KICKOFF", get_phase(w.context()) == Phase.OPP_KICKOFF)
    w.tick(90)
    bad = []
    for p in w.players:
        if p.id == w.store.gk_id:
            continue
        x, y = w.poses[p.id][0], w.poses[p.id][1]
        if math.hypot(x, y) < F.circle_radius - 0.02:
            bad.append((p.id, "中圈内", round(math.hypot(x, y), 2)))
        if x > 0.02:
            bad.append((p.id, "越半场", round(x, 2)))
    report("S6 非门将中圈外 + 本方半场", not bad, f"{bad}")
    report("S6 球未被推离中圈",
           math.hypot(w.ball[0], w.ball[1]) < 0.05,
           f"球=({w.ball[0]:.2f},{w.ball[1]:.2f})")
    print("     位置:", {p.id: (round(w.poses[p.id][0], 2),
                              round(w.poses[p.id][1], 2)) for p in w.players},
          "gk=", w.store.gk_id)


def s6b_opp_kickoff_illegal_start():
    """压测:从非法位置(已越半场/在中圈里)起步,不能崩。

    注意这**不是真实场景** —— 真实比赛里 READY 阶段就已经把人摆到合法位置了。
    这里只验证鲁棒性:非法起步时要么走出来变合法,要么把球碰走导致球提前
    进入比赛(phase 转 NORMAL,此后越半场是允许的)。两者都算正常。
    """
    w = World()
    w.set_ours((1, (-1.0, 1.0, 0.0)), (2, (0.5, -1.0, 0.0)), (3, (-6.0, 0.0, 0.0)))
    w.set_opps((1, (1.0, 0.0, 3.14)), (2, (2.0, 1.0, 3.14)), (3, (3.0, -1.0, 3.14)))
    w.set_ball(0.0, 0.0)
    w.make_game(secondary=10, kicking_team=2)
    w.tick(300)
    # 球被碰走 = 已进入比赛,此后越半场合法;只有"球还在中圈"时才要求站位合法
    ball_in_play = math.hypot(w.ball[0], w.ball[1]) > 0.15
    report("S6b 非法起点不崩", True,
           f"ball_in_play={ball_in_play} 球=({w.ball[0]:.2f},{w.ball[1]:.2f})")
    if not ball_in_play:
        bad = []
        for p in w.players:
            if p.id == w.store.gk_id:
                continue
            x, y = w.poses[p.id][0], w.poses[p.id][1]
            if math.hypot(x, y) < F.circle_radius - 0.02:
                bad.append((p.id, "中圈内", round(math.hypot(x, y), 2)))
            if x > 0.02:
                bad.append((p.id, "越半场", round(x, 2)))
        report("S6b 球仍在死球状态则站位合法", not bad, f"{bad}")


# ======================================================================
# S7 READY
# ======================================================================
def s7_ready():
    for kick_team, tag in ((1, "我方开球"), (2, "对方开球")):
        w = World()
        w.set_ours((1, (0.0, 0.0, 0.0)), (2, (1.0, 1.0, 0.0)), (3, (2.0, -1.0, 0.0)))
        w.set_opps((1, (0.0, 2.0, 3.14)), (2, (1.0, -2.0, 3.14)), (3, (2.0, 0.0, 3.14)))
        w.set_ball(0.0, 0.0)
        w.make_game(state=GameState.READY, kicking_team=kick_team)
        report(f"S7 READY({tag}) phase=READY", get_phase(w.context()) == Phase.READY)
        # 220 帧(约 7.3s):速度指令加了变化率限幅(防摔),起步是 0.4s 的斜坡,
        # 到点时间比原来的阶跃慢约 0.3~0.5m 行程,给足收敛时间。
        w.tick(220)
        bad = []
        for p in w.players:
            if p.id == w.store.gk_id:
                continue
            x, y = w.poses[p.id][0], w.poses[p.id][1]
            if x > 0.02:
                bad.append((p.id, "越半场", round(x, 2)))
            if kick_team == 2 and math.hypot(x, y) < F.circle_radius - 0.02:
                bad.append((p.id, "中圈内", round(math.hypot(x, y), 2)))
            if abs(x) > 7.0 or abs(y) > 4.5:
                bad.append((p.id, "场外", (round(x, 2), round(y, 2))))
        report(f"S7 READY({tag}) 站位合法", not bad, f"{bad}")
        print(f"     {tag}:", {p.id: (round(w.poses[p.id][0], 2),
                                     round(w.poses[p.id][1], 2)) for p in w.players})


# ======================================================================
# S8 少人 / 无球 / 无裁判机 / 有人被罚下
# ======================================================================
def s8_robust():
    w = World(n_ours=2)
    w.set_ours((1, (0.0, 1.0, 0.0)), (2, (-6.0, 0.0, 0.0)))
    w.set_opps((1, (2.0, 0.0, 3.14)), (2, (3.0, 1.0, 3.14)), (3, (4.0, -1.0, 3.14)))
    w.set_ball(1.0, 0.5)
    w.make_game()
    try:
        w.tick(60)
        report("S8 2 人队不崩", True, f"动作={[p.action for p in w.players]}")
    except Exception as e:  # noqa: BLE001
        report("S8 2 人队不崩", False, repr(e))

    w2 = World()
    w2.set_ours((1, (0.0, 1.0, 0.0)), (2, (1.0, 0.0, 0.0)), (3, (-6.0, 0.0, 0.0)))
    w2.set_ball(1.0, 0.5)
    w2.game = None
    try:
        w2.tick(30)
        report("S8 无 game 不崩", True, f"phase={get_phase(w2.context()).value}")
    except Exception as e:  # noqa: BLE001
        report("S8 无 game 不崩", False, repr(e))

    w3 = World()
    w3.set_ours((1, (0.0, 1.0, 0.0)), (2, (1.0, 0.0, 0.0)), (3, (-6.0, 0.0, 0.0)))
    w3.make_game()
    w3.no_ball = True
    try:
        w3.tick(30)
        report("S8 无球不崩", True, f"动作={[p.action for p in w3.players]}")
    except Exception as e:  # noqa: BLE001
        report("S8 无球不崩", False, repr(e))

    w4 = World()
    w4.set_ours((1, (0.0, 1.0, 0.0)), (2, (1.0, 0.0, 0.0)), (3, (-6.0, 0.0, 0.0)))
    w4.set_ball(1.0, 0.5)
    w4.make_game()
    w4.penalized = {2}
    try:
        w4.tick(60)
        report("S8 一人罚下不崩", True, f"动作={[p.action for p in w4.players]}")
    except Exception as e:  # noqa: BLE001
        report("S8 一人罚下不崩", False, repr(e))


# ======================================================================
# S9 推球控制器:8 种起手位置
# ======================================================================
def s9_push():
    cases = [
        ("A 正后方", (-2.0, 0.0, 0.0), (0.0, 0.0)),
        ("B 球正前方", (1.0, 0.0, 3.14), (0.0, 0.0)),
        ("C 侧方贴球", (0.0, 0.8, 0.0), (0.0, 0.0)),
        ("D 朝向反", (-1.5, 0.0, 3.14), (0.0, 0.0)),
        ("E 远端斜向", (-3.0, 3.0, 0.5), (0.0, 0.0)),
        ("F 球靠边线", (-1.5, 3.0, 0.0), (0.0, 3.5)),
        ("G 球在门前", (5.0, 0.0, 0.0), (6.0, 0.0)),
        ("H 远侧且球在前方", (4.0, 2.0, 3.14), (0.0, 0.0)),
    ]
    for name, (rx, ry, rth), (bx, by) in cases:
        w = World()
        w.set_ours((1, (rx, ry, rth)), (2, (-5.0, 3.0, 0.0)), (3, (-6.0, 0.0, 0.0)))
        w.set_opps((1, (5.0, -3.0, 3.14)), (2, (6.0, 3.0, 3.14)), (3, (4.0, 0.0, 3.14)))
        w.set_ball(bx, by)
        w.make_game()
        modes = {}
        for _ in range(600):
            w.tick(1)
            modes[w.players[0].action] = modes.get(w.players[0].action, 0) + 1
        report(f"S9 {name} 把球推进对方门区", w.ball[0] > 5.0,
               f"球=({w.ball[0]:.2f},{w.ball[1]:.2f}) 模式={modes}")


# ======================================================================
# S10 贴边线场景的指令上限 / NaN
# ======================================================================
def s10_limits():
    bad = []
    w = World()
    w.set_ours((1, (0.0, 4.4, 0.0)), (2, (0.0, -4.4, 0.0)), (3, (-6.0, 0.0, 0.0)))
    w.set_opps((1, (1.0, 0.0, 3.14)), (2, (2.0, 1.0, 3.14)), (3, (3.0, -1.0, 3.14)))
    w.set_ball(0.5, 4.4)
    w.make_game()
    for _ in range(300):
        w.tick(1)
        for p in w.players:
            b = p._backend
            if abs(b.vx) > param.MAX_LINEAR + 1e-6:
                bad.append(("vx", p.id, round(b.vx, 3)))
            if abs(b.w) > param.MAX_ANGULAR + 1e-6:
                bad.append(("w", p.id, round(b.w, 3)))
            if b.bad_values:
                bad.append(("nan", p.id))
    report("S10 贴边线场景指令不越上限", not bad, f"{bad[:5]}")


# ======================================================================
# S11 定位球 aim 与 support 一致性(传球不能传给空气)
# ======================================================================
def s11_setplay_consistency():
    cases = [
        (SetPlay.GOAL_KICK, (-6.0, 2.0)),
        (SetPlay.THROW_IN, (2.0, 4.4)),
        (SetPlay.THROW_IN, (2.0, -4.4)),
        (SetPlay.INDIRECT_FREE_KICK, (0.0, 0.0)),
        (SetPlay.DIRECT_FREE_KICK, (-2.0, 1.0)),
        (SetPlay.CORNER_KICK, (6.95, 4.45)),
    ]
    w = World()
    w.make_game()
    for sp, (bx, by) in cases:
        ctx = w.context()
        ball = BallState(x=bx, y=by)
        support = _set_play_support_point(ctx, sp, ball)
        aim, power = _set_play_aim(ctx, sp, ball, support)
        in_field = abs(aim[0]) <= 7.0 and abs(aim[1]) <= 4.5
        # 传球型定位球必须瞄队友,射门型(角球/直接任意球)允许瞄别处
        need_match = sp in (SetPlay.GOAL_KICK, SetPlay.THROW_IN,
                            SetPlay.INDIRECT_FREE_KICK)
        match = (not need_match) or (
            abs(aim[0] - support[0]) < 1e-6 and abs(aim[1] - support[1]) < 1e-6
        )
        far_enough = math.hypot(aim[0] - bx, aim[1] - by) > 1.5
        report(f"S11 {sp.value} @({bx},{by}) 落点场内/瞄队友/够远",
               in_field and match and far_enough,
               f"aim=({aim[0]:.2f},{aim[1]:.2f}) support=({support[0]:.2f},"
               f"{support[1]:.2f}) power={power}")


# ======================================================================
# S12 解围:本方危险区 + 被压迫 → attack:clear
# ======================================================================
def s12_clear():
    w = World()
    # 站位要对准球(球必须在体坐标前方 35° 锥角内),否则踢不到、只能推
    w.set_ours((1, (-5.5, 0.0, 0.0)), (2, (-3.0, 2.5, 0.0)), (3, (-6.0, 0.0, 0.0)))
    w.set_opps((1, (-4.4, 0.0, 0.0)), (2, (-2.0, -1.5, 0.0)), (3, (2.0, 1.0, 0.0)))
    w.set_ball(-5.0, 0.0)
    w.make_game()
    w.tick(2)
    acts = [p.action for p in w.players]
    report("S12 危险区被压迫 → 解围", any("clear" in a for a in acts), f"{acts}")
    # 解围方向必须朝对方半场(x 增大),不能往自家门踢
    d = w.last_kick_dir
    report("S12 解围方向朝前场",
           d is not None and math.cos(d) > 0.3,
           f"dir={None if d is None else round(math.degrees(d), 1)}°")
    w.tick(40)
    report("S12 球被踢出危险区", w.ball[0] > -4.0,
           f"球=({w.ball[0]:.2f},{w.ball[1]:.2f})")


# ======================================================================
# S13 传球:射门被封 + 队友位置更好 → attack:pass
# ======================================================================
def s13_pass():
    w = World()
    w.set_ours((1, (0.5, 0.0, 0.0)), (2, (4.0, 2.5, 0.0)), (3, (-6.0, 0.0, 0.0)))
    w.set_opps((1, (3.0, 0.3, 3.14)), (2, (5.0, 0.5, 3.14)),
               (3, (5.0, -0.5, 3.14)), (4, (6.5, 0.0, 3.14)))
    w.set_ball(1.0, 0.0)
    w.make_game()
    w.tick(2)
    acts = [p.action for p in w.players]
    report("S13 射门被三人+门将封死 → 传球",
           any("pass" in a for a in acts), f"{acts}")
    if w.last_kick_dir is not None:
        # 传给队友(4.0,2.5) → 方向应该朝右上
        want = math.atan2(2.5 - 0.0, 4.0 - 1.0)
        err = abs(math.degrees(math.atan2(
            math.sin(w.last_kick_dir - want), math.cos(w.last_kick_dir - want))))
        report("S13 传球方向朝队友", err < 25.0, f"误差={err:.1f}°")
    else:
        report("S13 传球方向朝队友", False, "没踢")


# ======================================================================
# S14 比分/时间自适应
# ======================================================================
def s14_match_mode():
    from src.player import match_mode
    cases = [
        # (我方, 对方, 剩余秒, 期望)
        ((0, 0), 300, "normal"),
        ((0, 1), 300, "press"),
        ((1, 0), 300, "hold"),
        ((2, 0), 300, "park"),
        ((0, 1), 20, "allin"),
        ((1, 0), 20, "park"),
        ((0, 0), 20, "steady"),
    ]
    for (sc, secs, want) in cases:
        w = World()
        w.make_game(score=sc, secs=secs)
        got = match_mode(w.context())
        report(f"S14 比分{sc} 剩余{secs}s → {want}", got == want, f"got={got}")

    # 直接调 support(),不受角色分派和球被推动的干扰。
    # 注意:diff=1 且非终场是 "hold"(收缩但仍按球权),不是 "park";
    #       "park" 要 diff>=2 或终场领先。
    for score, secs, ball_x, want_tag, tag in (
        ((2, 0), 300, 2.0, "support:park", "大比分领先"),
        ((1, 0), 20, 2.0, "support:park", "终场领先"),
        ((0, 0), 300, 0.5, "support:push_up", "平局且我方控球"),
        ((0, 1), 20, 0.5, "support:allin", "终场落后"),
        ((1, 0), 300, -1.0, "support:drop", "领先1球且球在中线后"),
    ):
        w = World()
        w.set_ours((1, (0.3, 0.0, 0.0)), (2, (2.5, 2.0, 0.0)), (3, (-6.0, 0.0, 0.0)))
        w.set_opps((1, (4.5, 0.0, 3.14)), (2, (4.0, 2.0, 3.14)),
                   (3, (5.0, -2.0, 3.14)))
        w.set_ball(ball_x, 0.0)
        w.make_game(score=score, secs=secs)
        p = w.players[1]          # id 2 = Support
        p.context = w.context()
        p.support()
        report(f"S14 {tag} → {want_tag}", p.action == want_tag, f"act={p.action}")


# ======================================================================
# S15 对方定位球按类型布防
# ======================================================================
def s15_opp_set_by_type():
    cases = [
        (SetPlay.CORNER_KICK, (6.95, 4.45)),
        (SetPlay.THROW_IN, (2.0, 4.4)),
        (SetPlay.DIRECT_FREE_KICK, (0.0, 0.0)),
        (SetPlay.GOAL_KICK, (-6.0, 2.0)),
    ]
    for sp, (bx, by) in cases:
        w = World()
        w.set_ours((1, (-2.0, 1.0, 0.0)), (2, (-2.0, -1.0, 0.0)), (3, (-6.0, 0.0, 0.0)))
        w.set_opps((1, (5.0, 1.0, 3.14)), (2, (4.0, 2.0, 3.14)), (3, (3.0, -1.0, 3.14)))
        w.set_ball(bx, by)
        w.make_game(set_play=sp, kicking_team=2)
        # 180 帧(6s):同样为速度限幅留收敛时间(见 S7 注释)。
        w.tick(180)
        gk = w.store.gk_id
        gk_x, gk_y = w.poses[gk][0], w.poses[gk][1]
        gk_on_line = abs(gk_x + F.length / 2.0) <= 0.12 and abs(gk_y) <= 1.3
        # 门将在门线上时命中 R-07「门线防守」豁免,可以离球 <1.45m
        bad = []
        for p in w.players:
            d = math.hypot(w.poses[p.id][0] - w.ball[0],
                           w.poses[p.id][1] - w.ball[1])
            if p.id == gk and gk_on_line:
                continue
            if d < 1.45:
                bad.append((p.id, round(d, 2)))
        report(f"S15 {sp.value} 全员避让 ≥1.45（门将豁免除外）",
               not bad, f"{bad}")
        report(f"S15 {sp.value} 门将贴门线拿豁免", gk_on_line,
               f"gk={gk} @({gk_x:.2f},{gk_y:.2f})")
        report(f"S15 {sp.value} 无人处于踢球状态",
               not any(p.is_kicking for p in w.players),
               f"{[p.action for p in w.players]}")


# ======================================================================
# S16 定位球直接射门:只有张角够的才射
# ======================================================================
def s16_setplay_shot():
    cases = [
        # (类型, 球位, 是否应该射, 说明)
        (SetPlay.THROW_IN, (5.0, 4.4), True, "对方半场边线,张角 13°"),
        (SetPlay.THROW_IN, (2.0, 4.4), True, "界外球中场,射程提到 14m 允许远射"),
        (SetPlay.CORNER_KICK, (6.95, 4.45), False, "角球张角 0.41°"),
        (SetPlay.GOAL_KICK, (-6.0, 2.0), True, "球门球,射程提到 14m 允许长射"),
        (SetPlay.INDIRECT_FREE_KICK, (5.0, 0.0), False, "间接任意球不能直接得分"),
        (SetPlay.DIRECT_FREE_KICK, (4.0, 0.0), True, "直接任意球近门"),
    ]
    for sp, (bx, by), want, why in cases:
        w = World()
        w.set_ours((1, (bx - 1.0, by + 1.0, 0.0)), (2, (0.0, 2.0, 0.0)),
                   (3, (-6.0, 0.0, 0.0)))
        w.set_opps((1, (6.5, 0.0, 3.14)), (2, (5.0, 3.0, 3.14)), (3, (3.0, -2.0, 3.14)))
        w.set_ball(bx, by)
        w.make_game(set_play=sp, kicking_team=1)
        w.tick(2)
        shot = any("setplay:shoot" in p.action for p in w.players)
        report(f"S16 {sp.value}@({bx},{by}) 射={want} ({why})",
               shot == want, f"got={shot} {[p.action for p in w.players]}")


# ======================================================================
# S17 摔倒 / 起身（规则 M-04：倒地过久 10s 罚下 30s）
# ======================================================================
def s17_get_up():
    # ① 持续摔倒 6s：get_up 只该下发 3 次左右（0 / 2.5 / 5.0s），
    #    绝不能每帧都发（那会把起身动作反复重启 → 永远爬不起来）
    w = World()
    w.set_ours((1, (-2.0, 0.0, 0.0)), (2, (1.0, 2.0, 0.0)), (3, (-6.0, 0.0, 0.0)))
    w.set_opps((1, (2.0, 0.5, 3.14)), (2, (4.0, -2.0, 3.14)), (3, (6.0, 1.0, 3.14)))
    w.set_ball(0.5, 0.0)
    w.make_game()
    for p in w.players:
        p._backend.fall_down_state = "fallen"
    w.tick(180)                                    # 6 s
    calls = w.players[0]._backend.get_up_calls
    report("S17 摔倒 6s 内 get_up 下发 2~3 次（不刷屏）", 2 <= calls <= 3,
           f"calls={calls}")
    moving = [
        (p.id, round(p._backend.vx, 2), round(p._backend.w, 2))
        for p in w.players
        if abs(p._backend.vx) > 1e-6 or abs(p._backend.w) > 1e-6
    ]
    report("S17 摔倒时速度指令归零", not moving, f"{moving}")
    report("S17 摔倒时动作标签=fallen",
           all(p.action == "fallen" for p in w.players),
           f"{[p.action for p in w.players]}")

    # ② 状态是 getting_up（正在起身）→ 一次都不该再下发 get_up
    w = World()
    w.set_ours((1, (-2.0, 0.0, 0.0)), (2, (1.0, 2.0, 0.0)), (3, (-6.0, 0.0, 0.0)))
    w.set_opps((1, (2.0, 0.5, 3.14)), (2, (4.0, -2.0, 3.14)), (3, (6.0, 1.0, 3.14)))
    w.set_ball(0.5, 0.0)
    w.make_game()
    for p in w.players:
        p._backend.fall_down_state = "getting_up"
    w.tick(90)                                     # 3 s
    calls = sum(p._backend.get_up_calls for p in w.players)
    report("S17 正在起身时不重复下发 get_up", calls == 0, f"calls={calls}")

    # ③ 站起来（fall_down_state 回到 normal）后，要等 GET_UP_SETTLE_S 才切 walk
    w = World()
    w.set_ours((1, (-2.0, 0.0, 0.0)), (2, (1.0, 2.0, 0.0)), (3, (-6.0, 0.0, 0.0)))
    w.set_opps((1, (2.0, 0.5, 3.14)), (2, (4.0, -2.0, 3.14)), (3, (6.0, 1.0, 3.14)))
    w.set_ball(0.5, 0.0)
    w.make_game()
    p = w.players[0]
    p._backend.fall_down_state = "fallen"
    p._backend.mode = "stand"
    w.tick(2)
    report("S17 摔倒后下发过 get_up", p._backend.get_up_calls == 1,
           f"calls={p._backend.get_up_calls}")
    p._backend.fall_down_state = "normal"          # 站起来了
    w.tick(30)                                     # 1s < GET_UP_SETTLE_S
    report("S17 起身后 1s 内不急着切 walk（防打断）",
           p._backend.mode != "walk", f"mode={p._backend.mode}")
    w.tick(60)                                     # 再等 2s
    report("S17 起身稳定后切回 walk",
           p._backend.mode == "walk", f"mode={p._backend.mode}")


# ======================================================================
# S18 防摔:切模式节流 + 速度指令限幅（"开局就倒"的两个真凶）
# ======================================================================
def s18_no_fall_startup():
    # ① 切模式节流：SDK 迟迟不进入 walk（mode 一直回报 stand），
    #    6s 内 request_mode 只该下发 ~4 次（每 MODE_RETRY_S=1.5s 一次），
    #    而不是每帧一次（180 次）—— 每帧 set_mode 会把步态控制器反复重置。
    w = World()
    w.set_ours((1, (-2.0, 0.0, 0.0)), (2, (1.0, 2.0, 0.0)), (3, (-6.0, 0.0, 0.0)))
    w.set_opps((1, (2.0, 0.5, 3.14)), (2, (4.0, -2.0, 3.14)), (3, (6.0, 1.0, 3.14)))
    w.set_ball(0.5, 0.0)
    w.make_game()
    for p in w.players:
        p._backend.mode = "stand"          # 假装 SDK 卡在 stand，永不进 walk
        p._backend.ignore_mode_request = True
    w.tick(180)                            # 6 s = 180 帧
    calls = w.players[0]._backend.mode_calls
    report("S18 切模式被节流(6s 内 ≤5 次,不是每帧)", 1 <= calls <= 5,
           f"calls={calls} (每帧下发会是 180 次)")
    report("S18 切模式期间不动(未就绪必须停车)",
           all(abs(p._backend.vx) < 1e-6 and abs(p._backend.w) < 1e-6
               for p in w.players),
           f"{[(p.id, round(p._backend.vx, 3)) for p in w.players]}")

    # ② 速度指令限幅：第一帧绝不能是满速。0 → MAX_LINEAR 需要 ~0.4s 斜坡。
    w = World()
    w.set_ours((1, (-4.0, 0.0, 0.0)), (2, (1.0, 2.0, 0.0)), (3, (-6.0, 0.0, 0.0)))
    w.set_opps((1, (2.0, 0.5, 3.14)), (2, (4.0, -2.0, 3.14)), (3, (6.0, 1.0, 3.14)))
    w.set_ball(2.0, 0.0)
    w.make_game()
    b = w.players[0]._backend
    b.vel_history.clear()
    w.tick(1)
    first = b.vel_history[0] if b.vel_history else (0.0, 0.0, 0.0)
    # 一帧允许的最大增量 = VEL_ACC_MAX / 30 ≈ 0.117
    report("S18 首帧速度不超过加速度限幅", abs(first[0]) <= 0.13 + 1e-6,
           f"首帧 vx={first[0]:.3f} (满速会是 1.4)")
    report("S18 首帧角速度不超过加速度限幅", abs(first[2]) <= 0.17 + 1e-6,
           f"首帧 w={first[2]:.3f} (满速会是 1.5)")

    # ③ 斜坡是"有限斜率"：任意相邻两帧的速度增量都不超过限幅
    w.tick(90)
    jumps = []
    for prev, cur in zip(b.vel_history, b.vel_history[1:]):
        dv = max(abs(cur[0] - prev[0]), abs(cur[1] - prev[1]))
        jumps.append(dv)
    worst = max(jumps) if jumps else 0.0
    report("S18 全程无速度阶跃(相邻帧增量 ≤0.13)", worst <= 0.13 + 1e-6,
           f"最大增量={worst:.3f}")

    # ④ 急停不走斜坡：SET/STOPPED 要求静止，必须立即归零
    w = World()
    w.set_ours((1, (-4.0, 0.0, 0.0)), (2, (1.0, 2.0, 0.0)), (3, (-6.0, 0.0, 0.0)))
    w.set_opps((1, (2.0, 0.5, 3.14)), (2, (4.0, -2.0, 3.14)), (3, (6.0, 1.0, 3.14)))
    w.set_ball(2.0, 0.0)
    w.make_game()
    w.tick(60)                             # 先跑起来，攒出速度
    w.make_game(state=GameState.PLAYING, stopped=True)
    w.tick(1)
    moving = [(p.id, round(p._backend.vx, 3), round(p._backend.w, 3))
              for p in w.players
              if abs(p._backend.vx) > 1e-6 or abs(p._backend.w) > 1e-6]
    report("S18 急停立即归零(不走斜坡)", not moving, f"{moving}")


# ======================================================================
# S19 长时压力:随机相位/比分/时间跑 3000 帧，检查 NaN 与速度阶跃
# ======================================================================
def s19_stress():
    rng = random.Random(20261023)
    w = World()
    w.set_ours((1, (-2.0, 0.0, 0.0)), (2, (1.0, 2.0, 0.0)), (3, (-6.0, 0.0, 0.0)))
    w.set_opps((1, (2.0, 0.5, 3.14)), (2, (4.0, -2.0, 3.14)), (3, (6.0, 1.0, 3.14)))
    w.set_ball(0.0, 0.0)
    w.make_game()

    states = (GameState.PLAYING, GameState.READY, GameState.SET,
              GameState.INITIAL, GameState.PLAYING)
    sets = (SetPlay.NONE, SetPlay.CORNER_KICK, SetPlay.THROW_IN,
            SetPlay.GOAL_KICK, SetPlay.DIRECT_FREE_KICK,
            SetPlay.INDIRECT_FREE_KICK, SetPlay.PENALTY_KICK)
    max_jump = 0.0
    nan = 0
    try:
        for i in range(3000):
            if i % 200 == 0:
                st = rng.choice(states)
                sp = rng.choice(sets) if st == GameState.PLAYING else SetPlay.NONE
                w.make_game(
                    state=st, set_play=sp,
                    kicking_team=rng.choice((0, 1, 2)),
                    stopped=rng.random() < 0.15,
                    score=(rng.randint(0, 3), rng.randint(0, 3)),
                    secs=rng.choice((300, 120, 45, 20, 5)),
                )
            prev = {p.id: (p._backend.vx, p._backend.vy, p._backend.w)
                    for p in w.players}
            w.tick(1)
            for p in w.players:
                b = p._backend
                for v in (b.vx, b.vy, b.w):
                    if not math.isfinite(v):
                        nan += 1
                a = prev[p.id]
                # 只看"变快"的方向:急停/减速到 0 是安全的(规则 M-01/M-02
                # 本来就要求静止),真正会摔的是速度突然变大或方向突翻。
                for cur, old in ((b.vx, a[0]), (b.vy, a[1]), (b.w, a[2])):
                    if abs(cur) > abs(old):
                        max_jump = max(max_jump, abs(cur) - abs(old))
        ok = True
    except Exception as e:  # noqa: BLE001
        ok = False
        report("S19 3000 帧压力测试不崩", False, f"{type(e).__name__}: {e}")
    if ok:
        report("S19 3000 帧压力测试不崩", True, "无异常")
    report("S19 全程无非有限速度值", nan == 0, f"nan={nan}")
    # 上限取 VEL_STEP_MAX(0.25) —— 踢球期间底盘被占用,恢复时 dt 会变大,
    # 所以单次步长的硬上限才是真正的约束(见 param.py 2c)。
    report("S19 全程无速度突增(单帧提速 ≤0.25)", max_jump <= 0.25 + 1e-6,
           f"最大突增={max_jump:.3f}")


# ======================================================================
def main():
    for fn in (s1_normal, s2_opp_set_play, s3_our_corner, s4_our_kickoff,
               s5_stopped, s6_opp_kickoff, s6b_opp_kickoff_illegal_start,
               s7_ready, s8_robust, s9_push,
               s10_limits, s11_setplay_consistency,
               s12_clear, s13_pass, s14_match_mode, s15_opp_set_by_type,
               s16_setplay_shot, s17_get_up, s18_no_fall_startup,
               s19_stress):
        print("=" * 78)
        fn()
    print("=" * 78)
    if ISSUES:
        print(f"\n共 {len(ISSUES)} 项异常:")
        for it in ISSUES:
            print("  " + it)
    else:
        print("\n全部通过,未发现异常。")


if __name__ == "__main__":
    main()
