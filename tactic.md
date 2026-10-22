# 3V3 仿真足球赛 · 战术策略与实现规格

> **文档定位**：本文档既是**给人看的战术手册**，也是**给 AI 看的实现规格**。
> 每一节都同时回答两个问题：**为什么这么踢**（战术意图）和**代码怎么跑**（坐标/公式/代码片段）。
>
> **适用环境**：Booster Studio 仿真与真机部署共用同一套代码，本文档中的坐标、公式、参数在两种环境下完全一致。
>
> **代码边界**：前期只改 `src/main.py`、`src/param.py`、`src/player.py` 三个文件。`src/framework/` 与 `src/utils/` 不动（除非后期专项优化）。

---

## 如何阅读本文档

| 你是 | 优先读 | 怎么用 |
| :--- | :----- | :----- |
| **人（教练视角）** | 第 1 章、每模块的「战术意图」、第 4 章 | 判断这套打法是否是你想要的，然后拍板 |
| **AI（执行视角）** | 第 2 章、每模块的「代码规格」、第 5 章参数表 | 直接按规格改 `main.py` / `param.py` / `player.py` |
| **双方都要** | 第 0 章铁律、第 6 章路线图、第 7 章验收清单 | 改动前看铁律，改完后照验收清单自测 |

**AI 执行约定**：文档中「代码规格」块里的 Python 可直接粘贴进对应文件；凡标注 `【新增参数】` 的常量必须先加进 `param.py`，再在 `main.py` 中使用（`main.py` / `player.py` 顶部已有 `from .param import *`）。

---

# 第 0 章　铁律、坐标系与场上常量

## 0.1 代码边界（不可越界）

| 层级 | 文件 | 是否改 | 职责 |
| :--- | :--- | :--- | :--- |
| 战术层 | `src/main.py` | ✅ **主战场** | Phase 状态机、角色分派、站位公式 |
| 参数层 | `src/param.py` | ✅ **主战场** | 所有可调数字，一处改全局生效 |
| 动作层 | `src/player.py` | ✅ 可加动作 | 单机器人高层动作（走位/踢球/技术动作） |
| 工具层 | `src/utils/` | ❌ 前期不动 | 几何、避障、A* |
| 平台层 | `src/framework/` | ❌ 前期不动 | 30Hz 循环、Context 构造、SDK 下发 |

> **为什么死守边界**：`framework/` 与 `utils/` 是公共基建，动一处会牵连所有行为；战术问题 95% 都能在 `main.py` 里用「站位公式 + 角色分派」解决。

## 0.2 坐标系（进攻视角，永远不变）

平台自动做坐标系转换，**你只需要写一套「往 +x 进攻」的逻辑**。

```
                     对方球门 (+7, 0)
                    ┌─────────────────┐
        y=+4.5  ────┤                 ├────  ← 边线
                    │    ● 中圈 r=1.5 │
        y=  0   ────┤       (0,0)     ├────  ← 中线
                    │                 │
        y=-4.5  ────┤                 ├────
                    └─────────────────┘
                     己方球门 (-7, 0)
                  -x ←────────────→ +x（进攻方向）
```

- **+x 朝对方球门**，**-x 朝己方球门**，原点 `(0,0)` 是场地中心。
- `theta = 0` 表示机器人面朝 +x（面朝对方门）；`theta = π` 表示面朝己方门。
- 无论你在场上实际朝左还是朝右进攻，策略代码里的坐标逻辑不变。

## 0.3 场上常量速查（写公式时直接代值）

| 含义 | 表达式 | 值 |
| :--- | :--- | :--- |
| 场地半长 `HL` | `field.length / 2` | **7.0** |
| 场地半宽 `HW` | `field.width / 2` | **4.5** |
| 对方门线 x | `+HL` | **+7.0** |
| 己方门线 x | `-HL` | **-7.0** |
| 球门半宽 | `field.goal_width / 2` | **1.3**（门内 y ∈ [-1.3, 1.3]） |
| 中圈半径 | `field.circle_radius` | **1.5** |
| 己方大禁区 | `x ∈ [-7, -4]`, `y ∈ [-3, 3]` | `penalty_area_length=3.0`, `width=6.0` |
| 己方小禁区 | `x ∈ [-7, -6]`, `y ∈ [-2, 2]` | `goal_area_length=1.0`, `width=4.0` |
| 守门员 home | `own_goal_area_center(ctx)` | **(-6.5, 0)** |
| 射门目标点 | `opponent_goal(ctx)` | **(7.25, 0)**（含 0.25 门内深度） |
| 己方门中心 | `own_goal(ctx)` | **(-7.0, 0)** |

**AI 注意**：所有站位公式一律用 `ctx.field.*` 表达式写，**不要硬编码 7.0 / 4.5**，这样换场地尺寸也不会失效。

## 0.4 合规红线（违反 = 罚下 30s / 取消资格）

这些是**代码里的硬约束**，任何战术优化都不得突破：

| 编号 | 红线 | 代码含义 |
| :--- | :--- | :--- |
| 🔴 **R-1** | `STOPPED` / `SET` 阶段**绝对不能移动** | 这些 Phase 里只能 `p.stop()`，不能 `walk_to` / `attack`。规则：SET 宽限后 1s 内路径 >0.15m 即罚下 |
| 🔴 **R-2** | 对方定位球（我方 `OPP_SET_PLAY`）**必须距球 ≥ 1.45m** | 代码用 **1.65m**（`OPPONENT_RESTART_AVOID_M`）留安全余量 |
| 🔴 **R-3** | 对方定位球主罚方触球前，我方**不得先触球** | `OPP_SET_PLAY` 中**禁止调用 `attack()`**（会去抢球）→ 必须整体重写，见 3.6 |
| 🔴 **R-4** | 对方开球时**必须在本方半场 + 中圈外** | 站位需满足 `x ≤ -1.5-0.3` 或 `dist(站位, (0,0)) ≥ 1.5+0.3` |
| 🔴 **R-5** | 不得触碰仿真内部接口 | 只读 `Context`，只用 `Player` 的公开动作 |
| 🔴 **R-6** | 踢球力度 ≤ 10（超过会摔倒） | `kick()` 内部已 clamp 到 `[KICK_POWER_MIN, KICK_POWER_MAX]` |

> ⚠️ **最危险的现成坑**：当前 `_act_opp_set_play()` 直接调用 `_act_normal()`，会让机器人**冲过去抢对方的定位球**，触发 R-2/R-3，被罚下 30 秒。**这是必须优先修复的 Bug**，见 3.6。

---

# 第 1 章　宏观战术：这套球队怎么踢

## 1.1 战术哲学（三条总纲）

**① 防守优先，进球靠转换。**
3V3 场地小、回合快，一次失位基本就是一个丢球。全队永远保留 **1 名门将 + 1 名补防**，绝不三人同时压上。丢球期望值比进球期望值更容易控制。

**② 用「站位公式」代替「追球」。**
三个机器人不要都追球（会挤成一团）。正确形态是：**一人逼抢（Attacker），一人守门（Guard），一人卡在球与球门之间或前插接应（Support）**，三点形成一个动态三角形，三角形随球移动。

**③ 死球阶段是白送的得分机会。**
定位球时双方站定、对手不能干扰，你有 10s（开球）～45s（其他）从容布阵。把 `OUR_SET_PLAY`、`OUR_KICKOFF` 写成「预设剧本」，是拉开比分性价比最高的手段。

## 1.2 阵型：1-1-1 动态三角

```
       对方门 (+7)
          │
   [Support]             ← 前插/补防线（随球权切换含义）
          │
        ● 球
          │
   [Attacker]            ← 逼抢者（离球最近）
          │
   [Guard]               ← 门将（永远在门线前，护住球门）
       己方门 (-7)
```

- **Attacker**：离球最近者，职责是**抢到球并做决策**（射门 / 传球 / 解围）。
- **Guard**：护门者，站在「球 → 己方门中心」方向、距门线 0.6～1.0m 处，**随球的 y 值左右滑动**（不是站死在禁区中央）。
- **Support**：**含义随球权切换**——
  - 我方控球（进攻）→ 站到 Attacker **前侧方**形成接应三角，准备接传球；
  - 对方控球（防守）→ 站到 **球 → 己方门** 连线上距球 3m 处，形成第二道防线。

## 1.3 角色定义表（ meeting.md 要求的「角色六要素」）

| 要素 | Attacker（主攻） | Guard（门将） | Support（支援） |
| :--- | :--- | :--- | :--- |
| **定义** | 离球最近、可执行动作者 | 离己方门最近者，锁定后尽量不换 | 剩下的那个人 |
| **站位** | 球后方 0.35m（球在人与对方门之间） | 门线前 0.6m，y 跟随球的 y | 进攻：Attacker 前侧方 2.5m；防守：球→门连线距球 3m |
| **状态（阶段）** | NORMAL 全程；定位球时是主罚者 | 所有阶段（含 STOPPED 站定） | NORMAL；定位球时跑战术位 |
| **分工** | 抢球 → 判断射/传/解围 → 执行 | 护门线、封堵射门角度、球门球开球 | 接应、补防、二点球 |
| **动作处理** | 见 2.4「力度/角度决策树」 | 面向球，横向跟随；威胁大时前顶 | 面向球，走位避障 |
| **走位** | 走 `_behind_ball()` 计算的球后点 | 走门线滑动点 | 走接应点或封堵点 |

## 1.4 球权与分区（Zone）

### 1.4.1 分区定义（用 `ball.x` 判）

| Zone | 判据 | 名称 | 全队姿态 |
| :--- | :--- | :--- | :--- |
| **Z0** | `ball.x < -4.0` | 本方禁区危急 | **全员回收**，优先解围，不组织 |
| **Z1** | `-4.0 ≤ ball.x < -1.5` | 本方后场 | 守势，Support 回撤成第二后卫 |
| **Z2** | `-1.5 ≤ ball.x ≤ 1.5` | 中场 | 标准 1-1-1 站位 |
| **Z3** | `1.5 < ball.x ≤ 4.0` | 对方前场 | 攻势，Support 前插 |
| **Z4** | `ball.x > 4.0` | 对方禁区 | **全线压上**，敢投兵力 |

### 1.4.2 球权判据

```python
# 我方控球 = 我方最近者比对方最近者明显更接近球
our_best = min(dist(p.pose, ball) for p in active)
opp_best = min(dist(r.pose, ball) for r in ctx.opponents.values() if r.pose)
our_possession = our_best < opp_best - 0.2     # 【新增参数】POSSESSION_MARGIN_M = 0.2
```

## 1.5 三级决策链（宏观 → 微观的骨架）

```
第 1 级：Phase 状态机   →  现在是开球？定位球？还是正常拼抢？（第 3 章）
第 2 级：Zone 分区      →  球在哪个区？决定阵型松紧与投入兵力（3.7 + 第 4 章）
第 3 级：场景修正        →  比分？剩余时间？有人被罚下？（第 4 章）
                              ↓
                        输出：三个机器人的目标点 + 动作
```

> 关键点：**不要新增顶层 Phase**。所有复杂战术都在已有 Phase **内部**再做判断（这也是高分队伍的通用做法）。

---

# 第 2 章　微观执行：代码每一帧怎么跑

## 2.1 一帧（1/30 秒）里发生了什么

```
runtime._tick()
  ├─ _build_context()  构造只读 Context（球/队友/对手/裁判机，带新鲜度过滤）
  ├─ p.context = ctx   把 Context 塞给每个 Player
  ├─ agent.play(ctx, players, store)   ← ★ 你的代码从这里进入
  │     ├─ get_phase(ctx)         算出当前 Phase
  │     ├─ 活性自理               对每个 p：ensure_ready()（摔倒起身/切 walk 模式）
  │     │                        被罚下 / 未就绪 / 无 pose 的踢出本帧 active 列表
  │     ├─ 按 Phase 分派到 _act_*   ← ★ 战术逻辑（第 3 章）
  │     └─ 画可视化
  └─ debugdraw.flush()
```

**AI 必知**：
- `play()` 每帧被调用，**无状态**（除了 `store`）。每帧都要重新算一遍目标点。
- `active` 列表已过滤掉「被罚下 / 摔倒 / 位置未知」的球员，**角色分派只能在 `active` 里做**。
- 每帧对每个球员**有且仅有一个动作**（走位 or 踢球 or 停止）。不要在同一帧给同一个 `p` 调两次动作。

## 2.2 走位原语 `walk_to()` 详解（最重要）

```python
p.walk_to(target, face=None, avoid_ball=False, avoid_robots=False, arrive_dist=ARRIVE_DIST)
```

### 2.2.1 内部两模式（决定机器人怎么动）

设 `distance = dist(pose, target)`，`heading` = 朝目标的方向（含避障修正）。

| 模式 | 触发条件 | 行为 |
| :--- | :--- | :--- |
| **到达** | `distance < arrive_dist`（默认 0.15m） | 若给了 `face`：转到 `face`（误差 >0.1rad 才转），否则 `stop()`。**返回 True** |
| **近距离·全向** | `distance ≤ OMNI_DIST`（1.0m） | 把位移向量转到**体坐标系**，同时平移 + 转向 `face`。斜着走也能到位 |
| **远距离·转身-走** | `distance > OMNI_DIST`（1.0m） | ① 角差 `\|heading - theta\| > TURN_THRESHOLD`(0.5rad≈28.6°) → **原地转**；<br>② 角差小 → `vx = clamp(1.5·distance·cos(err), 0, 2.0)` **边走边微调朝向** |

### 2.2.2 ⚠️ 关键陷阱：`face` 在远距离被完全忽略

看源码：远距离分支只用了 `heading` 和 `angle_err`，**根本没读 `face`**。`face` 只在「近距离」和「已到达」时生效。

**后果**：如果你写 `p.walk_to(远处的点, face=某个角度)`，机器人在路上**不会**按 `face` 调整朝向，只有到了终点才会转身。

**正确做法**（要控制朝向就二选一）：
- **方案 A（推荐）**：把目标点放在**你希望它走的方向上**，让 `heading` 自然等于你要的朝向（`heading = atan2(target-pose)`）。
- **方案 B**：拆成两步——先 `p.face_to(目标朝向)` 转到位，再 `walk_to(target, face=朝向)`。

## 2.3 【示例】向前走 x 米 → 掉头 180° → 再走 n 米

这是你举的例子，也是**所有精确走位的通用套路**：机器人没有「前进 x 米」指令，只有「走到某个坐标点」。所以必须**把「走多少米」翻译成「走到哪个坐标」**。

### 2.3.1 坐标换算公式

设当前位姿 `(px, py, theta)`，把「沿朝向走 d 米」翻译成目标点：

```python
target = (px + d * math.cos(theta), py + d * math.sin(theta))
```

「掉头 180°」= 新朝向 `theta_back = normalize_angle(theta + math.pi)`，于是第二个点：

```python
p2 = (p1x + n * math.cos(theta_back), p1y + n * math.sin(theta_back))
   = (p1x - n * math.cos(theta),      p1y - n * math.sin(theta))     # 因为 cos(θ+π) = -cosθ
```

### 2.3.2 场景一：n 是「已知要走的距离」

```python
def advance_then_retreat(p, x: float, n: float) -> None:
    """沿当前朝向前进 x 米 → 掉头 180° → 再走 n 米。每帧调用，靠 walk_to 返回值推进。"""
    pose = p.pose
    if pose is None:
        p.stop(); return

    # ① 第一个目标点：沿朝向前进 x 米
    p1 = (pose.x + x * math.cos(pose.theta),
          pose.y + x * math.sin(pose.theta))
    if not p.walk_to(p1, face=pose.theta):
        return                      # 还没走到 P1，本帧结束

    # ② 到达 P1：计算掉头后的朝向与第二个目标点
    back = normalize_angle(pose.theta + math.pi)
    p2 = (pose.x + n * math.cos(back),
          pose.y + n * math.sin(back))
    # 此时目标点在正后方 → heading 与当前朝向差 ~180°
    # → 触发远距离「原地转」分支（角差 π > 0.5rad），机器人先转身再走 ✅
    p.walk_to(p2, face=back)
```

### 2.3.3 场景二：**n 需要通过当前坐标计算**（你强调的情况）

典型需求：「走到距对方球门 1.0m 处停」，沿某方向 `dir` 走，需要走多远？用**投影法**：

```python
def distance_along(px, py, dir_theta, tx, ty) -> float:
    """从 (px,py) 沿 dir_theta 方向，走到含 (tx,ty) 的垂足，需要走多少米。"""
    ux, uy = math.cos(dir_theta), math.sin(dir_theta)
    return (tx - px) * ux + (ty - py) * uy     # 向量点积 = 投影长度
```

**实战例子：Attacker 沿射门方向推进，但要停在球后 0.35m（CHASE_BEHIND_M）处**

框架已有这个工具 —— `_behind_ball(ball_x, ball_y, aim, offset)`：

```python
# 语义：从球沿「背离 aim」方向退 offset 米 → 得到「球后方的站位」
# 内部就是投影/单位向量的反向：
ux, uy = (aim - ball) / |aim - ball|           # 球指向目标
return (ball_x - ux * offset, ball_y - uy * offset)   # 往反方向退
```

**自己写「沿某方向走 n 米」的通用模板**：

```python
def walk_along(p, dir_theta, n):
    """沿场地绝对方向 dir_theta 走 n 米（n 由坐标算出来）。"""
    pose = p.pose
    target = (pose.x + n * math.cos(dir_theta), pose.y + n * math.sin(dir_theta))
    p.walk_to(target, face=dir_theta)

# 例：想知道「沿进攻方向走到距对方门线 1.2m 处」要走多远
HL = ctx.field.length / 2.0
stop_x = HL - 1.2
n = distance_along(pose.x, pose.y, 0.0, stop_x, pose.y)   # 沿 +x 方向
n = max(0.0, n)                                          # 负数说明已经越过，不再前进
walk_along(p, 0.0, n)
```

### 2.3.4 场景三：要求「严格先转 180°，再直线走」（用 `store` 做两阶段）

方案 A 中机器人是「边转边走」。若要**先精确转到位再直走**，必须用跨帧状态：

```python
# init_store 里加：store.retreat_stage = "turn"
def strict_advance_then_retreat(context, p, store, x, n):
    pose = p.pose
    if pose is None:
        p.stop(); return

    if store.retreat_stage == "forward":
        p1 = (pose.x + x*math.cos(pose.theta), pose.y + x*math.sin(pose.theta))
        if p.walk_to(p1, face=pose.theta):
            store.retreat_stage = "turn"          # 到达 P1 → 进入掉头阶段

    elif store.retreat_stage == "turn":
        back = normalize_angle(pose.theta + math.pi)
        err = abs(normalize_angle(back - pose.theta))
        if err < 0.08:                            # 【新增参数】FACE_TOL_RAD = 0.08
            store.retreat_stage = "back"
        else:
            p.face_to(back)                       # 原地精确转 180°

    elif store.retreat_stage == "back":
        back = normalize_angle(pose.theta + math.pi)
        p2 = (pose.x + n*math.cos(back), pose.y + n*math.sin(back))
        p.walk_to(p2, face=back)                  # 已是该朝向 → 直接直走
```

> **AI 记住**：任何「分几步走」的动作，都要在 `store` 里存一个阶段名，靠 `walk_to()` 的返回值推进。这是本项目写复杂动作的唯一套路。

## 2.4 踢球原语 `kick()` 详解

### 2.4.1 接口与坐标系转换（必须理解）

```python
p.kick(kick_direction, power)
```

- `kick_direction`：**场地绝对角度**（相对 +x 轴，弧度），例如 `0.0` = 把球踢向对方门方向。
- `power`：力度，内部 clamp 到 `[1.0, 10.0]`。**>10 会摔倒**。

`kick()` 内部做了两件事（你不用管，但要理解）：

```python
# ① 球的位置：场地坐标 → 体坐标（相对机器人）
ball_x_body =  dx * cos(theta) + dy * sin(theta)
ball_y_body = -dx * sin(theta) + dy * cos(theta)
# ② 踢球方向：场地角 → 体坐标角
direction_body = normalize_angle(kick_direction - pose.theta)
```

> **结论**：你只管「把球踢向场地的哪个方向」，`theta` 不用你操心。

### 2.4.2 力度模型（meeting.md：不同距离/角度力度要有变化）

当前 `plan_kick()` 固定踢向球门中心、力度固定 5.0 —— 这是「不会踢球」的表现。改成**按距离与意图动态计算**：

| 意图 | 触发条件 | 力度公式 | 建议范围 |
| :--- | :--- | :--- | :--- |
| **射门** | 有射门角度 | `power = clamp(2.5 + 0.30 * d_goal, 3.0, 8.0)` | d=3m→3.4；d=10m→5.5 |
| **传球** | 队友位置更好 | `power = clamp(1.5 + 0.55 * d_pass, 2.0, 6.0)` | d=2m→2.6；d=6m→4.8 |
| **解围** | 球在 Z0 且被逼抢 | 固定 **7.5～8.5**（大力） | 别超 9 |
| **开球短传** | `OUR_KICKOFF` | 固定 **3.0**（传给 2～4m 外接应点） | 别用 5.0 大力 |
| **轻推** | 调整球位 | **2.0** | — |

其中 `d_goal = dist(ball, opponent_goal)`，`d_pass = dist(ball, 接球点)`。

> **调参直觉**：力度越大球越快越远，但**超过 8 机器人容易踢飞或自己摔倒**。近距离射门用小力（够进门即可，更可控），远距离才加力。

### 2.4.3 射门点选择（不要永远踢球门中心）

球门中心通常站着对方门将。改为在门内**采样若干候选点，打分选最优**：

- **候选点**：在对方门内按 `GOAL_AIM_FRACTIONS = (-0.8, -0.4, 0.0, 0.4, 0.8)` 取 5 个 y（占门半宽的比例；门半宽 1.3 → y ∈ [-1.04, 1.04]，自动留出门柱余量，换场地也不失效）。
- **打分**：`score = -0.5·|y|`（越靠中路角度容错越大）`− SHOT_BLOCK_PENALTY`（射门线被对手挡住时扣分）。
- **「被挡住」的判据**：对手到射线的**垂直距离** `< SHOT_BLOCK_RADIUS_M`(0.45) **且** 沿射线的投影 `t > 0`（在球的前方，不是身后）。

> **完整实现见 3.7.3** `Player._best_shot_point()` 与 `Player._perp_dist()`。
> 放在 `player.py` 而不是 `main.py`，是因为 `attack()` → `plan_kick()` 这条链路在 `Player` 内部，需要直接调用它。

### 2.4.4 射门 / 传球 / 解围 决策树

```
我方 Attacker 持球（距球 < KICK_ENTER_M）
  │
  ├─ 球在 Z0（本方禁区）且附近有对手压迫 → 【解围】踢向 (2.0, ±3.5) 远离对手的一侧，power 8.0
  │
  ├─ 计算最佳射门点 best_shot，得分 shot_score
  │    ├─ shot_score 高（无封堵）且 dist(ball,对方门) < 8.0 → 【射门】
  │    │      direction = angle_to(ball, best_shot)
  │    │      power = clamp(2.5 + 0.30*d_goal, 3.0, 8.0)
  │    └─ 被封堵，但有队友在「更靠近对方门 + 其射门线较空」的位置
  │           → 【传球】 direction = angle_to(ball, 队友前方0.6m提前量)
  │              power = clamp(1.5 + 0.55*d_pass, 2.0, 6.0)
  │
  └─ 都不满足 → 【带球推进】朝对方门方向轻推（power 3.0）后继续追
```

**传球提前量**：目标点 = 队友位置沿「队友→对方门」方向前移 0.6m，避免传到脚下被断。

```python
# 【新增参数】PASS_LEAD_M = 0.6
def _pass_target(context, teammate_pose):
    goal = opponent_goal(context)
    d = angle_to(teammate_pose.x, teammate_pose.y, *goal)
    return (teammate_pose.x + PASS_LEAD_M * math.cos(d),
            teammate_pose.y + PASS_LEAD_M * math.sin(d))
```

## 2.5 `attack()` 追球-射门链路（现行实现）

```python
def attack(self, kick_target=None):
    d = dist(self.pose, ball)
    self._kicking = d <= (KICK_EXIT_M if self._kicking else KICK_ENTER_M)   # 迟滞
    if self._kicking:
        direction, power = self.plan_kick()      # ← ★ 改成 2.4 的决策树
        self.kick(direction, power)
    else:
        self.release_kick()
        self.walk_to(_behind_ball(ball.x, ball.y, kick_target, CHASE_BEHIND_M))
```

**要点**：
- **迟滞**：距球 ≤ `KICK_ENTER_M`(2.0) 进入踢球；进入后要 > `KICK_EXIT_M`(2.5) 才退出。防边界抖动（反复进入/退出踢球状态）。
- **追球点**：`_behind_ball()` 算出的点是「球 → 目标」连线上、球的**后方** 0.35m。机器人站到那里时，**球自然夹在它与球门之间**，朝向天然对准射门方向 —— 这就是「绕到球后」的实现，不需要手动算角度。
- **改法**：把 `plan_kick()` 里「固定踢球门中心 + 固定力度」替换成 2.4.3 / 2.4.4 的逻辑即可。

## 2.6 `store` 跨帧记忆的四种模式

`store` 是 `SimpleNamespace`，跨整场比赛存活，是写复杂战术的唯一工具。

| 模式 | 用途 | 本项目实例 |
| :--- | :--- | :--- |
| **保持状态** | 决定一次，执行到底 | `store.kickoff_taker`（锁定开球主罚者，避免每帧重选导致抖动） |
| **迟滞** | 该换才换 | `store.normal_attacker` + `ATTACKER_KEEP_DIST_MARGIN_M`(0.3)：新候选必须近 0.3m 以上才换人 |
| **计数/冷却** | 干完歇一会 | 踢完球后 0.5s 内不再踢（防连续触发） |
| **阶段边沿** | 只在切换瞬间做一次 | `store.prev_phase != Phase.OUR_KICKOFF` → 进入开球时重选主罚者 |

**AI 必加的 store 字段**（第 3 章会用到）：

```python
def init_store(self, store) -> None:
    store.prev_phase = None
    store.cur_phase = None
    store.kickoff_taker = None
    store.normal_attacker = None
    # ↓ 新增
    store.guard_id = None            # 锁定的门将 id（避免门将频繁换人）
    store.setplay_taker = None       # 定位球主罚者
    store.setplay_plan = None        # 定位球剧本（选了哪个战术分支）
    store.retreat_stage = "forward"  # 多段走位的阶段
    store.last_ball = None           # 上一帧球位（算球速/预测）
    store.last_ball_t = 0.0
```

## 2.7 避障与路径规划（走位时怎么用）

- `walk_to(avoid_ball=True, avoid_robots=True)` 开启后：先把球/机器人/球门结构收集成圆形障碍，再走 **A\* 全局规划**（`USE_GLOBAL_PATH_PLANNER=True`），A\* 失败才退回局部 VFH 扫描。
- **什么时候开**：
  - ✅ 走**站位点**（READY / 定位球布阵 / Support / Guard）→ 开，避免撞球、撞人。
  - ❌ `attack()` 追球 → **关**（要直取球，不能绕）。
  - ❌ 定位球主罚者接近球 → 关（要去踢球，不能把球当障碍绕开）。

> **坑**：给主罚者开 `avoid_ball=True` 会导致它绕着球走、永远踢不到球。主罚者必须 `avoid_ball=False`。

---

# 第 3 章　逐 Phase 战术与代码规格

## 3.0 七种 Phase 总览 + 公共工具

| Phase | 触发（`get_phase`） | 战术目标 | 关键红线 | 现状 |
| :--- | :--- | :--- | :--- | :--- |
| `STOPPED` | `SET`(非开球) / `INITIAL` / `FINISHED` / `stopped` | 站定不动 | 🔴 R-1 不能动 | ✅ 已实现 |
| `READY` | `state == READY` | 走预设阵型 | 站位要为 SET 合法性做准备 | ⚠️ 需改（下标分配/硬编码） |
| `OUR_KICKOFF` | `PLAYING` + `secondary_time>0` + 我方主罚 | 二过一开出，不丢球权 | 直接射门进球无效（G-03） | ⚠️ 需改 |
| `OPP_KICKOFF` | `PLAYING` + `secondary_time>0` + 对方主罚 | 退防 + 封堵出球线 | 🔴 R-4 本方半场+中圈外 | ⚠️ 需改 |
| `OUR_SET_PLAY` | `PLAYING` + `set_play≠NONE` + 我方主罚 | 预设剧本得分 | 10s(kick)/45s 内发出 | 🔴 空白（现调 normal） |
| `OPP_SET_PLAY` | `PLAYING` + `set_play≠NONE` + 对方主罚 | 退避 1.65m + 布防 | 🔴🔴 R-2/R-3 不能碰球 | 🔴 **Bug：会抢球被罚下** |
| `NORMAL` | `PLAYING` 其他 | 1-1-1 攻防 | — | ⚠️ 需改（分区/射门/守门） |

### 3.0.1 公共工具函数（先加进 `main.py`，后面各 Phase 复用）

#### ⚠️ 先补齐 `main.py` 的 import（否则会 `NameError`）

当前 `main.py` 顶部只导入了 `dist / opponent_goal / own_goal`，第 3 章会用到更多。请先改成：

```python
# src/main.py 顶部
import math
from .param import *
from .player import Player, _behind_ball            # ← 新增 _behind_ball
from .utils import dist, opponent_goal, own_goal
from .utils.geom import angle_to, clamp, normalize_angle   # ← 新增这一行
```

> `_behind_ball` 定义在 `player.py` 里（模块级函数，`attack()` 也在用它），`main.py` 要显式导入才能用。

#### 工具函数

```python
# ============ 站位合法化（合规保险丝，所有站位点都必须过这一层）============
# 【新增参数】LEGAL_MARGIN_M = 0.3
def _clamp_legal(context, pt, *, own_half=False, out_circle=False):
    """把站位点夹到『场地内 + 可选本方半场 / 可选中圈外』。

    own_half=True   → 强制 x <= -LEGAL_MARGIN_M（本方半场，READY/对方开球用）
    out_circle=True → 强制距中心 >= circle_radius + LEGAL_MARGIN_M（中圈外）
    """
    f = context.field
    HL, HW = f.length / 2.0, f.width / 2.0
    x = clamp(pt[0], -HL + LEGAL_MARGIN_M, HL - LEGAL_MARGIN_M)
    y = clamp(pt[1], -HW + LEGAL_MARGIN_M, HW - LEGAL_MARGIN_M)
    if own_half:
        x = min(x, -LEGAL_MARGIN_M)                      # 绝不越中线
    if out_circle:
        r = f.circle_radius + LEGAL_MARGIN_M             # 1.5 + 0.3 = 1.8
        d = math.hypot(x, y)
        if d < r:                                        # 在中圈内 → 沿径向外推
            if d < 1e-6:
                x = -r                                   # 退化：推向本方半场
            else:
                x, y = x / d * r, y / d * r
            if own_half:
                x = min(x, -LEGAL_MARGIN_M)              # 推出后可能又越线，再夹一次
    return (x, y)


# ============ 门将站位：门线前滑动（不是站死禁区中央）============
# 【新增参数】GUARD_LINE_OFFSET_M = 0.6   # 距门线多远（门前 0.6m）
# 【新增参数】GUARD_Y_GAIN = 0.6          # 门将 y 跟随球 y 的比例（<1 = 不全跟，防被晃）
# 【新增参数】GUARD_Y_LIMIT_M = 1.0       # 门将 y 的活动范围（门半宽 1.3，留 0.3）
def _guard_point(context):
    """门将目标点：球→己方门方向上，门前 GUARD_LINE_OFFSET_M，y 跟随球。"""
    ball = context.ball
    HL = context.field.length / 2.0
    gy = 0.0
    if ball is not None:
        gy = clamp(ball.y * GUARD_Y_GAIN, -GUARD_Y_LIMIT_M, GUARD_Y_LIMIT_M)
    return (-HL + GUARD_LINE_OFFSET_M, gy)


# ============ 把球踢向场地上任意一点（所有定位球 / 传球的统一入口）============
def _kick_towards(p, target, power):
    """把球踢向场地坐标 target。target 可以是点，也可以是 (x,y)。"""
    ball = p.context.ball if p.context is not None else None
    if ball is None or p.pose is None:
        p.stop(); return
    direction = angle_to(ball.x, ball.y, target[0], target[1])
    p.kick(direction, power)


# ============ 选门将并锁定（避免守门员每帧换人）============
def _select_guard(context, players, store):
    """门将 = 离己方门最近者；用 store.guard_id 锁定，除非他被罚下/不可动。"""
    active_ids = {p.id for p in players}
    if getattr(store, "guard_id", None) in active_ids:
        return next(p for p in players if p.id == store.guard_id)
    gx, gy = own_goal(context)
    g = min(players, key=lambda p: dist(p.pose.x, p.pose.y, gx, gy))
    store.guard_id = g.id
    return g
```

---

## 3.1 `STOPPED` —— 站定，什么都不做

### 战术意图
比赛未开始 / 摆球中 / 暂停 / 已结束。**乱动只会被罚下**（SET 阶段 1s 内移动 >0.15m 即罚下 30s），所以唯一正确动作是站定。

### 代码规格
```python
elif phase == Phase.STOPPED:
    _clear_normal_sticky(store)
    for p in active:
        p.action = "stopped"
        p.stop()
```

### 验收
- ✅ 进入 SET 后所有机器人 `vx=vy=vyaw=0`，位置 1s 内漂移 < 0.05m。
- ✅ 整场未被判 `MOTION_IN_SET`。

> **AI 注意**：`STOPPED` 期间**不要**调 `walk_to`（哪怕目标点就是当前位置），也不要 `attack`。只调 `p.stop()`。

---

## 3.2 `READY` —— 走预设阵型

### 战术意图
开赛前 / 进球后，双方走到指定站位。**不追球**。站位点必须让接下来的 `SET` 阶段合法（本方半场、非开球方中圈外），否则一进 SET 就被罚下。

### 判据
```python
our_kickoff = (game.kicking_team == context.team_id)
```

### 站位公式

| 我方 | 角色 | 目标点（表达式） | 我方开球时取值 | 对方开球时取值 |
| :--- | :--- | :--- | :--- | :--- |
| 是 | **Taker** | `(-circle_radius - 0.3, 0)` | (-1.8, 0) | — |
| 是 | **接应** | `(-3.0, ±2.0)`（选对手少的那侧） | (-3.0, ±2.0) | — |
| — | **Guard** | `_guard_point()` | (-6.4, 0) | (-6.4, 0) |
| 否 | **封堵A** | `(-circle_radius - 0.8, +1.4)` | — | (-2.3, +1.4) |
| 否 | **封堵B** | `(-circle_radius - 0.8, -1.4)` | — | (-2.3, -1.4) |

> 所有点最后统一过 `_clamp_legal(..., own_half=True, out_circle=对方开球时True)`，确保在场地内、本方半场、中圈外。

### 代码规格（替换现有 `_act_ready`）

```python
# 【新增参数】READY_TAKER_BACK_M = 0.3     # 主罚者站中圈后多远
# 【新增参数】READY_SUPPORT_X = -3.0       # 接应点 x
# 【新增参数】READY_SUPPORT_Y = 2.0        # 接应点 |y|
# 【新增参数】READY_BLOCK_BACK_M = 0.8     # 对方开球时封堵点退到中圈后多远
# 【新增参数】READY_BLOCK_Y = 1.4          # 封堵点 |y|

def _act_ready(context: Context, players: list[Player], store) -> None:
    """READY：按开球方布阵；站位点全部合法化，保证进 SET 不违例。"""
    if not players:
        return
    game = context.game
    our_kickoff = game is not None and game.kicking_team == context.team_id
    f = context.field

    # ① 先定门将（锁定，避免每帧换人）
    guard = _select_guard(context, players, store)
    rest = [p for p in players if p is not guard]

    # ② 门将走门线点
    guard.action = "ready:guard"
    guard.walk_to(_guard_point(context), face=0.0,
                  avoid_ball=True, avoid_robots=True)

    if not rest:
        return

    if our_kickoff:
        # ③ 我方开球：一人站中圈后当主罚者，另一人拉边接应
        taker = min(rest, key=lambda p: _player_dist_to_ball(context, p))
        rest2 = [p for p in rest if p is not taker]
        taker.action = "ready:taker"
        taker.walk_to(
            _clamp_legal(context, (-f.circle_radius - READY_TAKER_BACK_M, 0.0),
                         own_half=True),
            face=0.0, avoid_ball=True, avoid_robots=True)

        # 接应点选「对手较少」的一侧
        sy = _less_crowded_side(context) * READY_SUPPORT_Y
        for p in rest2:
            p.action = "ready:support"
            p.walk_to(
                _clamp_legal(context, (READY_SUPPORT_X, sy), own_half=True),
                face=0.0, avoid_ball=True, avoid_robots=True)
            sy = -sy                       # 多人时左右分开
    else:
        # ④ 对方开球：全部退到中圈外本方半场，左右分开站
        sy = READY_BLOCK_Y
        for p in rest:
            p.action = "ready:block"
            p.walk_to(
                _clamp_legal(context,
                             (-f.circle_radius - READY_BLOCK_BACK_M, sy),
                             own_half=True, out_circle=True),
                face=0.0, avoid_ball=True, avoid_robots=True)
            sy = -sy


def _less_crowded_side(context) -> float:
    """返回对手较少的一侧：+1 表示上半场(y>0)更空，-1 表示下半场更空。"""
    up = sum(1 for r in context.opponents.values()
             if r.pose is not None and r.pose.y > 0)
    down = sum(1 for r in context.opponents.values()
               if r.pose is not None and r.pose.y <= 0)
    return 1.0 if up <= down else -1.0
```

### 为什么改（对照原实现）

| 原实现问题 | 改法 |
| :--- | :--- |
| 用 `players[0]/[1]/[2]` **下标**分配 → 有人被罚下时错位，甚至越界 | 改为**角色分配**（先选门将，再按距离选主罚者） |
| 站位点硬编码、未做合法化 → 可能站进中圈/对方半场 | 全部过 `_clamp_legal(own_half=True, out_circle=True)` |
| 门将站位写死 `(-length/2 + goal_area_length, 0)` | 改用 `_guard_point()`，会随球滑动 |

### 验收
- ✅ 进球后重新 READY，三人都能走到各自点位，`arrive_dist` 内停住。
- ✅ 从 READY 转入 SET 后，**无人被判 `ILLEGAL_POSITIONING`**。
- ✅ 故意让一人被罚下（只剩 2 人）时，不报 `IndexError`，剩下两人正常布阵。

---

## 3.3 `OUR_KICKOFF` —— 我方开球：二过一，不丢球权

### 战术意图
规则 **G-03**：`kick-off` 直接进球**必须 ≥2 次稳定触球**才有效，否则重开且不加分。
➡️ **所以开球第一脚绝不能直接射门**，那是不可能得分的浪费。
正确打法：**主罚者轻传给接应者 → 接应者拿球 → 转入正常进攻**。这样既满足 2 次触球，又把球权稳稳送到前场。

### 判据
```python
Phase.OUR_KICKOFF   # PLAYING + secondary_time>0(10s倒计时) + kicking_team==我方
```
**注意**：球被踢动后 `secondary_time` 仍在倒数，所以本 Phase 会**持续约 10 秒**。必须判断球是否已开动：

```python
ball_moved = context.ball is not None and \
             dist(context.ball.x, context.ball.y, 0.0, 0.0) > CENTER_LEAVE_DIST_M   # 0.15m
```
- **球没动** → 执行开球站位与传球；
- **球已动** → 直接转 `_act_normal()`（球活了，进入正常攻防）。

### 角色与站位

| 角色 | 站位 / 动作 |
| :--- | :--- |
| **Taker** | 绕到球后（球 → 接应点 连线的后方 0.35m），到位后**轻传**给接应点，`power=3.0` |
| **接应者** | 提前站到接应点 `(-1.0, ±2.5)`（选对手少的那侧），面向球准备接球 |
| **Guard** | `_guard_point()` 守门，不参与开球 |

### 代码规格（替换现有 `_act_our_kickoff`）

```python
# 【新增参数】KICKOFF_PASS_POWER = 3.0       # 开球短传力度（原 5.0 太大）
# 【新增参数】KICKOFF_SUPPORT_X = -1.0       # 接应点 x（本方半场靠中）
# 【新增参数】KICKOFF_SUPPORT_Y = 2.5        # 接应点 |y|（拉边）
# 【新增参数】KICKOFF_STAGE_M = 1.2          # 主罚者绕后距离（原 2.0 太远，改为 1.2）

def _act_our_kickoff(context, players, store) -> None:
    if not players or context.ball is None:
        return

    # ① 球已开动 → 交回正常攻防（本 Phase 会持续到 secondary_time 归零）
    if dist(context.ball.x, context.ball.y, 0.0, 0.0) > CENTER_LEAVE_DIST_M:
        _act_normal(context, players, store)
        return

    # ② 锁定主罚者（只在进入本 Phase 的那一帧重选 → 边沿触发）
    active_ids = {p.id for p in players}
    if store.prev_phase != Phase.OUR_KICKOFF or store.kickoff_taker not in active_ids:
        store.kickoff_taker = _select_closest_attacker(context, players).id

    taker = next((p for p in players if p.id == store.kickoff_taker), None)
    if taker is None:
        _act_normal(context, players, store)
        return

    # ③ 门将（锁定）
    guard = _select_guard(context, players, store)
    guard.action = "kickoff:guard"
    guard.walk_to(_guard_point(context), avoid_ball=True, avoid_robots=True)

    # ④ 接应者：站到对手较少的那侧，面向球
    rest = [p for p in players if p is not taker and p is not guard]
    sy = _less_crowded_side(context) * KICKOFF_SUPPORT_Y
    for p in rest:
        p.action = "kickoff:support"
        p.walk_to(_clamp_legal(context, (KICKOFF_SUPPORT_X, sy), own_half=True),
                  face=angle_to(p.pose.x, p.pose.y,
                                context.ball.x, context.ball.y) if p.pose else None,
                  avoid_ball=True, avoid_robots=True)
        sy = -sy

    # ⑤ 主罚者：绕到「球 → 接应点」连线的球后方，到位后轻传
    aim = (KICKOFF_SUPPORT_X, _less_crowded_side(context) * KICKOFF_SUPPORT_Y)
    stage = _behind_ball(context.ball.x, context.ball.y, aim, KICKOFF_STAGE_M)
    d_ball = dist(taker.pose.x, taker.pose.y,
                  context.ball.x, context.ball.y) if taker.pose else math.inf

    if d_ball > KICK_ENTER_M:
        # 还在绕后：注意 avoid_ball=False（要去碰球，不能把球当障碍绕开）
        taker.action = "kickoff:stage"
        taker.walk_to(stage, avoid_ball=False, avoid_robots=True)
    else:
        # 已到球边：轻传给接应点（不射门！射门进球无效）
        taker.action = "kickoff:pass"
        _kick_towards(taker, aim, KICKOFF_PASS_POWER)
```

### 验收
- ✅ 开球后球**先传到接应者附近**（不是直接射向对方球门）。
- ✅ 我方开球后 5 秒内仍保有球权（未被对方断走）。
- ✅ 无 `ILLEGAL_POSITIONING` / `MOTION_IN_SET`。
- ✅ 开球 10s 内球已离开中圈（未因超时被判球权开放）。

---

## 3.4 `OPP_KICKOFF` —— 对方开球：退防 + 封堵第一脚出球线

### 战术意图
规则 **P-02 / R-4**：非开球方必须站**本方半场 + 中圈外**，否则罚下 30s。
战术目标不是「抢球」，而是：
1. **堵住对方第一脚出球线路**（站在对手与球门之间）；
2. 抢断后能**立刻由守转攻**（站的位置本身就是反击起点）。

### 站位公式（盯人 + 护门复合点）

对每个对方非开球者 `opp`，防守目标点 = **对手 → 己方球门** 连线上、距对手 1.2m（靠球门侧）：

```python
# 【新增参数】OPP_KICKOFF_MARK_DIST_M = 1.2   # 盯人时站在对手身后(靠我门侧)多远
def _mark_point(context, opp_pose, mark_dist):
    gx, gy = own_goal(context)
    d = angle_to(opp_pose.x, opp_pose.y, gx, gy)
    return (opp_pose.x + mark_dist * math.cos(d),
            opp_pose.y + mark_dist * math.sin(d))
```
算完必须过 `_clamp_legal(own_half=True, out_circle=True)`，否则会站进中圈被罚下。

### 代码规格（替换现有 `_act_opp_kickoff`）

```python
def _act_opp_kickoff(context, players, store) -> None:
    if not players:
        return

    # ① 门将（锁定）
    guard = _select_guard(context, players, store)
    guard.action = "opp_kickoff:guard"
    guard.walk_to(_guard_point(context), avoid_ball=True, avoid_robots=True)
    rest = [p for p in players if p is not guard]
    if not rest:
        return

    # ② 收集对方球员（不含开球者：开球者在中圈里，不用管）
    opps = [r for r in context.opponents.values()
            if r.pose is not None and r.pose.x < context.field.circle_radius]
    opps.sort(key=lambda r: r.pose.x)                 # 越靠我方球门越优先盯

    # ③ 贪心匹配：每个防守者挑「离自己最近的未分配对手」去盯
    used = set()
    for p in rest:
        best, best_d = None, math.inf
        for i, r in enumerate(opps):
            if i in used:
                continue
            d = dist(p.pose.x, p.pose.y, r.pose.x, r.pose.y)
            if d < best_d:
                best, best_d, best_i = r, d, i
        if best is None:
            # 没有可盯的对手 → 退到中圈外本方半场兜底
            target = _clamp_legal(context,
                                  (-context.field.circle_radius - 0.8, 0.0),
                                  own_half=True, out_circle=True)
        else:
            used.add(best_i)
            target = _clamp_legal(
                context, _mark_point(context, best.pose, OPP_KICKOFF_MARK_DIST_M),
                own_half=True, out_circle=True)
        p.action = "opp_kickoff:mark"
        p.walk_to(target,
                  face=angle_to(p.pose.x, p.pose.y,
                                context.ball.x, context.ball.y) if (p.pose and context.ball) else None,
                  avoid_ball=True, avoid_robots=True)
```

### 为什么改（对照原实现）

| 原实现问题 | 改法 |
| :--- | :--- |
| 只有 2 个写死点 `(-r-0.5,0)`、`(-r-2.0,0.5)`，**第 3 人无站位**（`zip` 截断） | 每个防守者都动态算点，人数不确定也不会漏 |
| 不盯人，对方开球后随便传 | 按对手位置盯人 + 护门，堵第一脚出球线 |
| 未做中圈外/半场合法化 | 全部过 `_clamp_legal(own_half=True, out_circle=True)` |

### 验收
- ✅ 对方开球全程，**我方无人被判 `ILLEGAL_POSITIONING`**（最关键）。
- ✅ 三人都在 `x < 0` 且 `dist((0,0)) ≥ 1.8`。
- ✅ 对方开球后第一脚传球路线上有我方球员（距离传球线 < 1.5m）。

---

## 3.5 `OUR_SET_PLAY` —— 我方定位球：按类型写剧本（当前是空白，必做）

### 战术意图
定位球时**双方站定、对手不能干扰**（防守方须退 1.45m），你有 **45 秒**（开球类 10s）从容布阵。这是「白送的得分窗口」。
核心套路：**先布阵 → 选主罚者 → 主罚者走到球后 → 按剧本踢向预定目标**。

### 判据与类型
```python
sp = get_set_play_type(context)     # THROW_IN / GOAL_KICK / CORNER_KICK / FREE_KICK...
```
用 `context.ball` 的实际坐标判断球在哪个区域（**不要假设**，裁判摆球位置以实际读到的为准）：
```python
in_own_half  = context.ball.x < 0
near_their_goal = context.ball.x > context.field.length/2 - 3.0     # x > 4.0
```

### 3.5.1 通用主罚流程（四种类型共用）

```python
# 【新增参数】SETPLAY_CHASE_BEHIND_M = 0.35   # 主罚者绕到球后多远（同 CHASE_BEHIND_M）
def _setplay_taker(context, p, aim, power):
    """定位球主罚者：未到位则绕到『球→aim』连线的球后方；到位则踢向 aim。"""
    ball = context.ball
    if ball is None or p.pose is None:
        p.stop(); return
    d = dist(p.pose.x, p.pose.y, ball.x, ball.y)
    if d > KICK_ENTER_M:
        # ⚠️ avoid_ball=False —— 要去踢球，绝不能把球当障碍绕开
        p.walk_to(_behind_ball(ball.x, ball.y, aim, SETPLAY_CHASE_BEHIND_M),
                  avoid_ball=False, avoid_robots=True)
    else:
        _kick_towards(p, aim, power)
```

### 3.5.2 四种剧本的战术与代码

| 类型 | 球在哪 | 战术目标 | 主罚目标点 `aim` | 力度 |
| :--- | :--- | :--- | :--- | :--- |
| **THROW_IN** 界外球 | 边线 `\|y\|≈4.5` | 快发 + 找接应，别丢球权 | 本方半场→踢向前方安全区；对方半场→踢向门前/中路 | 按距离 4~5 |
| **GOAL_KICK** 球门球 | 己方门区 `x≈-6, \|y\|≈2` | **安全出球**，绝不横传中路 | 同侧/异侧**边路前场** `(-2.0, ±3.5)`，选对手少的那侧 | 5~6 |
| **CORNER_KICK** 角球 | 对方角旗 `x≈+7, \|y\|≈4.5` | 传中 + **二点进攻** | 门前抢点区 `(HL-1.5, ∓0.8)` 对角传中 | 4~5 |
| **FREE_KICK** 任意球 | 犯规点 | 有角度就射，没角度就传 | 用 2.4.4 决策树 | 见 2.4.2 |

```python
# 【新增参数】SETPLAY_PASS_POWER = 4.5     # 定位球传球基准力度
# 【新增参数】SETPLAY_CLEAR_POWER = 7.5    # 后场解围力度
# 【新增参数】CORNER_AIM_X_BACK_M = 1.5    # 角球落点退到门线前多远
# 【新增参数】CORNER_AIM_Y = 0.8           # 角球落点 |y|
# 【新增参数】CORNER_SECOND_X_BACK_M = 3.0 # 二点(弧顶)埋伏点退到门线前多远
# 【新增参数】GK_OUT_X = -2.0              # 球门球出球点 x
# 【新增参数】GK_OUT_Y = 3.5               # 球门球出球点 |y|
# 【新增参数】THROW_IN_SAFE_X = 2.5        # 界外球安全出球向前推进距离

def _act_our_set_play(context, players, store) -> None:
    if not players or context.ball is None:
        return
    sp = get_set_play_type(context)
    ball = context.ball
    f = context.field
    HL = f.length / 2.0

    # ① 锁定主罚者（只在进入本 Phase 的第一帧重选 → 边沿触发，防抖动）
    active_ids = {p.id for p in players}
    if store.prev_phase != Phase.OUR_SET_PLAY or store.setplay_taker not in active_ids:
        store.setplay_taker = _select_closest_attacker(context, players).id
    taker = next((p for p in players if p.id == store.setplay_taker), None)
    if taker is None:
        _act_normal(context, players, store); return

    # ② 门将：球在我方半场时留守；球在对方半场时可适度前压（但仍不过中线）
    guard = _select_guard(context, players, store)
    if ball.x < 0:
        guard.action = "setplay:guard"
        guard.walk_to(_guard_point(context), avoid_ball=True, avoid_robots=True)
    else:
        guard.action = "setplay:guard_push"
        guard.walk_to(_clamp_legal(context, (-1.0, 0.0), own_half=True),
                      avoid_ball=True, avoid_robots=True)

    support = [p for p in players if p is not taker and p is not guard]
    side = _less_crowded_side(context)          # 对手较少的一侧：+1 / -1

    # ③ 按类型分剧本
    if sp == SetPlay.CORNER_KICK:
        # —— 角球：对角传中 + 二点埋伏 ——
        aim = (HL - CORNER_AIM_X_BACK_M, -math.copysign(CORNER_AIM_Y, ball.y))
        power = SETPLAY_PASS_POWER
        # 接应：一人抢前点，一人埋伏弧顶（二点）
        for i, p in enumerate(support):
            if i == 0:
                pt = (HL - CORNER_AIM_X_BACK_M, -math.copysign(CORNER_AIM_Y, ball.y))
                p.action = "setplay:corner_far"
            else:
                pt = (HL - CORNER_SECOND_X_BACK_M, math.copysign(1.0, ball.y))
                p.action = "setplay:corner_second"
            p.walk_to(_clamp_legal(context, pt), avoid_ball=True, avoid_robots=True)

    elif sp == SetPlay.GOAL_KICK:
        # —— 球门球：安全出球到边路前场，绝不横传中路 ——
        aim = (GK_OUT_X, side * GK_OUT_Y)
        power = SETPLAY_CLEAR_POWER * 0.8          # 约 6.0
        for p in support:
            p.action = "setplay:gk_outlet"
            p.walk_to(_clamp_legal(context, (GK_OUT_X - 1.0, side * GK_OUT_Y)),
                      avoid_ball=True, avoid_robots=True)
            side = -side

    elif sp == SetPlay.THROW_IN:
        # —— 界外球：快发，踢向前方安全区，接应者拉开 ——
        inward = -math.copysign(1.0, ball.y)       # 从边线指向场内
        if ball.x < 0:                             # 本方半场：稳，别丢
            aim = (ball.x + THROW_IN_SAFE_X, inward * 1.5)
        else:                                      # 对方半场：可以打门前
            aim = (HL - CORNER_AIM_X_BACK_M, inward * CORNER_AIM_Y)
        power = SETPLAY_PASS_POWER
        for p in support:
            p.action = "setplay:throw_outlet"
            p.walk_to(_clamp_legal(context, (ball.x + 1.5, inward * 2.5)),
                      avoid_ball=True, avoid_robots=True)

    else:
        # —— 任意球 / 其它：交给 Attacker 的射门-传球决策树（见 2.4.4） ——
        taker.action = "setplay:free"
        taker.attack()                             # attack() 内部用改进后的 plan_kick
        for p in support:
            p.action = "setplay:free_support"
            p.support()
        return

    # ④ 主罚者执行（前三种类型走这里）
    taker.action = f"setplay:{sp.value}"
    _setplay_taker(context, taker, _clamp_legal(context, aim), power)
```

### 验收
- ✅ 我方角球能把球送到对方门前 3m 内，且有队友在抢点区。
- ✅ 球门球**从不**出现「横传中路被断后直接丢球」。
- ✅ 主罚者能走到球边并踢出（不会绕着球打转 —— 检验 `avoid_ball=False`）。
- ✅ 45s 内完成发球（未因超时被判球权开放）。

---

## 3.6 `OPP_SET_PLAY` —— 对方定位球：退避 1.65m + 布防 🔴 **现存 Bug，优先修**

### 🚨 为什么必须重写
当前实现 `_act_opp_set_play()` **直接调用 `_act_normal()`**，而 `_act_normal()` 会让 Attacker 去 `attack()` 抢球。
这会同时触发两条红线：
- **R-2**：防守方距球 < 1.45m → 罚下 30s
- **R-3**：主罚方触球前防守方先触球 → 罚下 30s + 重踢

**结论：对方定位球时，我方一个球都不能碰，全部退到 1.65m 外站定布防。**

### 战术意图（按对方定位球类型）

| 对方类型 | 布防重点 |
| :--- | :--- |
| **CORNER_KICK** 对方角球 | 一人**堵近门柱**，一人**盯对方抢点者**，门将守门线 |
| **GOAL_KICK** 对方球门球 | 压上逼抢接球点（在我方半场之外也要压迫），但要退够 1.65m |
| **THROW_IN** 对方界外球 | **压迫接球点**：每个对方接应者旁站一人（1.65m 外） |
| **FREE_KICK** 对方任意球 | **排人墙**：站「球→己方门」连线上距球 1.65m，横向排开挡射门角 |

### 3.6.1 避让保险丝（所有布防点必须过这一层）

```python
# 【新增参数】OPP_SET_AVOID_M = 1.65     # 规则要求 1.45，取 1.65 留安全余量
# 【新增参数】WALL_SPACING_M = 0.6       # 人墙横向间距
def _push_away_from_ball(context, pt, avoid=OPP_SET_AVOID_M):
    """把点推到距球 >= avoid 的位置（沿『球→点』方向外推）。"""
    ball = context.ball
    if ball is None:
        return pt
    d = dist(pt[0], pt[1], ball.x, ball.y)
    if d >= avoid:
        return pt
    a = angle_to(ball.x, ball.y, pt[0], pt[1])
    if d < 1e-6:                                  # 退化：点就在球上，朝己方门方向让
        a = angle_to(ball.x, ball.y, *own_goal(context))
    return (ball.x + avoid * math.cos(a), ball.y + avoid * math.sin(a))


def _wall_points(context, n):
    """排人墙：站『球→己方门』连线距球 OPP_SET_AVOID_M 处，垂直方向排开 n 个位置。"""
    ball = context.ball
    gx, gy = own_goal(context)
    a = angle_to(ball.x, ball.y, gx, gy)          # 球指向球门
    base = (ball.x + OPP_SET_AVOID_M * math.cos(a),
            ball.y + OPP_SET_AVOID_M * math.sin(a))
    perp = a + math.pi / 2.0                      # 垂直于射门线
    pts = []
    for i in range(n):
        off = (i - (n - 1) / 2.0) * WALL_SPACING_M
        pts.append((base[0] + off * math.cos(perp),
                    base[1] + off * math.sin(perp)))
    return pts
```

### 3.6.2 代码规格（完整替换 `_act_opp_set_play`）

```python
def _act_opp_set_play(context, players, store) -> None:
    """对方定位球：全员退避 >=1.65m 布防。❌ 严禁调用 attack() / _act_normal()。"""
    if not players or context.ball is None:
        return
    sp = get_set_play_type(context)
    ball = context.ball
    f = context.field
    HL = f.length / 2.0

    # ① 门将守门线（规则 R-07 豁免：站本方门线上、y 在门宽内不算违例）
    guard = _select_guard(context, players, store)
    guard.action = "opp_set:guard"
    guard.walk_to(_clamp_legal(context, _guard_point(context)),
                  face=angle_to(guard.pose.x, guard.pose.y, ball.x, ball.y)
                       if guard.pose else None,
                  avoid_ball=True, avoid_robots=True)

    rest = [p for p in players if p is not guard]
    if not rest:
        return

    def _go(p, raw_pt, tag):
        """统一出口：先避让球的 1.65m，再夹进场地，再走过去。"""
        pt = _clamp_legal(context, _push_away_from_ball(context, raw_pt))
        p.action = f"opp_set:{tag}"
        p.walk_to(pt,
                  face=angle_to(p.pose.x, p.pose.y, ball.x, ball.y) if p.pose else None,
                  avoid_ball=True, avoid_robots=True)   # 面向球，随时准备反应

    if sp == SetPlay.CORNER_KICK:
        # ② 对方角球：一人堵近柱，其余盯对方抢点者
        near_post = ( -HL + 0.4, math.copysign(f.goal_width/2 - 0.3, ball.y) )
        _go(rest[0], near_post, "near_post")
        marks = [r for r in context.opponents.values()
                 if r.pose is not None and r.pose.x > 0]     # 对方压在前场的抢点者
        for i, p in enumerate(rest[1:]):
            if i < len(marks):
                _go(p, _mark_point(context, marks[i].pose, OPP_KICKOFF_MARK_DIST_M), "mark")
            else:
                _go(p, (-HL + 1.2, -math.copysign(1.0, ball.y)), "spare")

    elif sp == SetPlay.FREE_KICK or sp == SetPlay.DIRECT_FREE_KICK \
            or sp == SetPlay.INDIRECT_FREE_KICK or sp == SetPlay.PENALTY_KICK:
        # ③ 对方任意球：排人墙挡射门角
        pts = _wall_points(context, len(rest))
        for p, pt in zip(rest, pts):
            _go(p, pt, "wall")

    elif sp == SetPlay.THROW_IN:
        # ④ 对方界外球：压迫接球点（站在对方接应者旁 1.65m 外）
        marks = sorted([r for r in context.opponents.values() if r.pose is not None],
                       key=lambda r: dist(r.pose.x, r.pose.y, ball.x, ball.y))
        marks = marks[1:]                          # 第 0 个是主罚者本人，跳过
        for i, p in enumerate(rest):
            if i < len(marks):
                _go(p, _mark_point(context, marks[i].pose, OPP_KICKOFF_MARK_DIST_M), "press")
            else:
                _go(p, (-HL + 2.0, 0.0), "spare")

    else:
        # ⑤ 对方球门球 / 其它：压到中场，逼抢出球点
        for i, p in enumerate(rest):
            _go(p, (-HL + 3.0 + i * 1.0, (1.0 if i % 2 == 0 else -1.0) * 2.0), "press")
```

### 验收（最关键的一节）

- ✅ **整场不被判 `ILLEGAL_POSITIONING` / `MOTION_IN_SET`** —— 这是本节的生死线。
- ✅ 对方定位球期间，我方**每个**机器人到球的距离 **≥ 1.6m**（用可视化标签看 `dist`）。
- ✅ 我方**无人**进入踢球状态（画面上不出现 `[KICK]` 标记）。
- ✅ 对方任意球时，人墙确实挡在「球→己方门」的连线上（不是散在两边）。

---

## 3.7 `NORMAL` —— 正常拼抢：1-1-1 + 分区 + 射/传/解围决策

### 战术意图
`NORMAL` 占比赛 80% 以上时间，是胜负主战场。核心是**三点动态三角**：
- **Attacker** 逼抢并做「射门 / 传球 / 解围」三选一；
- **Guard** 在门线前**随球左右滑动**（不是站死禁区中央）；
- **Support** 按**球权 + 分区**在「补防」与「前插接应」之间切换。

### 3.7.1 角色分派（替换 `_act_normal`）

**分派顺序很重要**：先定门将，再选逼抢者 —— 这样门将不会因「离球最近」被拉去进攻，导致球门空虚。

```python
# 【新增参数】SUPPORT_ATTACK_MIN_X = -1.5    # 球的 x 超过它，Support 才允许前插
# 【新增参数】SUPPORT_ATTACK_AHEAD_M = 2.5   # 进攻接应点在 Attacker 前方多远
# 【新增参数】SUPPORT_ATTACK_SIDE_M = 1.5    # 进攻接应点横向拉开多远

def _act_normal(context, players, store) -> None:
    if not players or context.ball is None:
        return
    ball = context.ball

    # ① 先定门将（锁定，不参与 attacker 竞争）
    guard = _select_guard(context, players, store)

    # ② 危险区例外：球在己方禁区且门将恰是全场最近者 → 门将出击解围，另一人补门
    danger = ball.x < OWN_DANGER_X
    if danger:
        nearest = _select_closest_attacker(context, players)
        if nearest is guard:
            attacker = guard                                   # 门将出击
            rest = [p for p in players if p is not guard]
            if rest:                                           # 剩下的人补门
                gx, gy = own_goal(context)
                tmp = min(rest, key=lambda p: dist(p.pose.x, p.pose.y, gx, gy))
                tmp.action = "guard:cover"
                tmp.walk_to(_guard_point(context), avoid_ball=True, avoid_robots=True)
                support = [p for p in rest if p is not tmp]
            else:
                support = []
            attacker.action = "attack:clear"
            attacker.attack()
            for p in support:
                p.action = "support"
                p.walk_to(_support_point(context, attacker, False),
                          face=angle_to(p.pose.x, p.pose.y, ball.x, ball.y) if p.pose else None,
                          avoid_ball=True, avoid_robots=True)
            return

    # ③ 常规：门将守门；其余人中离球最近者当 attacker（带迟滞防抖动）
    rest = [p for p in players if p is not guard]
    if not rest:                                               # 只剩门将一人
        guard.action = "guard"
        guard.guard()
        return

    attacker = _select_closest_attacker(
        context, rest, getattr(store, "normal_attacker", None))
    store.normal_attacker = attacker.id
    attacker.action = "attack"
    attacker.attack()                                          # 内部走 3.7.3 决策树

    # ④ 门将
    guard.action = "guard"
    guard.guard()

    # ⑤ 其余为 support
    our_possession = _has_possession(context, players)
    for p in rest:
        if p is attacker:
            continue
        p.action = "support"
        p.walk_to(_support_point(context, attacker, our_possession),
                  face=angle_to(p.pose.x, p.pose.y, ball.x, ball.y) if p.pose else None,
                  avoid_ball=True, avoid_robots=True)


def _has_possession(context, players) -> bool:
    """我方是否控球：我方最近者明显比对方最近者更接近球。"""
    ball = context.ball
    if ball is None:
        return False
    ours = min((dist(p.pose.x, p.pose.y, ball.x, ball.y)
                for p in players if p.pose is not None), default=math.inf)
    theirs = min((dist(r.pose.x, r.pose.y, ball.x, ball.y)
                  for r in context.opponents.values() if r.pose is not None),
                 default=math.inf)
    return ours < theirs - POSSESSION_MARGIN_M


def _support_point(context, attacker, our_possession):
    """Support 站位：进攻时前插形成三角，防守时卡在球与球门之间。"""
    ball = context.ball
    if (not our_possession) or ball.x < SUPPORT_ATTACK_MIN_X or attacker.pose is None:
        # —— 防守位：球 → 己方门 连线上，距球 SUPPORT_DIST_M ——
        gx, gy = own_goal(context)
        a = angle_to(ball.x, ball.y, gx, gy)
        pt = (ball.x + SUPPORT_DIST_M * math.cos(a),
              ball.y + SUPPORT_DIST_M * math.sin(a))
    else:
        # —— 进攻位：Attacker 前侧方，形成接应三角 ——
        goal = opponent_goal(context)
        a = angle_to(attacker.pose.x, attacker.pose.y, goal[0], goal[1])
        side = 1.0 if attacker.pose.y <= 0 else -1.0       # 站到 attacker 的另一侧
        fx = attacker.pose.x + SUPPORT_ATTACK_AHEAD_M * math.cos(a)
        fy = attacker.pose.y + SUPPORT_ATTACK_AHEAD_M * math.sin(a)
        perp = a + side * math.pi / 2.0                    # 横向拉开
        pt = (fx + SUPPORT_ATTACK_SIDE_M * math.cos(perp),
              fy + SUPPORT_ATTACK_SIDE_M * math.sin(perp))
    return _clamp_legal(context, pt)
```

### 3.7.2 门将改造（`player.py` 的 `guard()`）

**原问题**：`guard()` 站死在小禁区中心 `(-6.5, 0)`，球打到边路时门将还在中间，等于空门。

```python
# player.py —— 替换 guard()
# 【新增参数】GUARD_LINE_OFFSET_M = 0.6   # 站门线前 0.6m
# 【新增参数】GUARD_Y_GAIN = 0.6          # y 跟随球的比例（<1 防被假动作晃开）
# 【新增参数】GUARD_Y_LIMIT_M = 1.0       # 门将横向活动范围（门半宽 1.3，留 0.3）
def guard(self) -> None:
    """守门：站『门前 GUARD_LINE_OFFSET_M』，y 随球滑动；始终面向球。"""
    ctx = self.context
    if ctx is None or self.pose is None:
        self.action = "guard:stop"
        self.stop()
        return
    ball = ctx.ball
    HL = ctx.field.length / 2.0
    gy = clamp(ball.y * GUARD_Y_GAIN, -GUARD_Y_LIMIT_M, GUARD_Y_LIMIT_M) if ball else 0.0
    home = (-HL + GUARD_LINE_OFFSET_M, gy)

    face = 0.0
    if GUARD_FACE_BALL and ball is not None:
        face = angle_to(self.pose.x, self.pose.y, ball.x, ball.y)

    self.action = "guard:line"
    debugdraw.point(home[0], home[1], rgb=(0.0, 0.6, 1.0), scale=0.2, ns="guard_home")
    self.walk_to(home, face=face, avoid_ball=True, avoid_robots=True)
```

> main.py 中所有 `guard.walk_to(_guard_point(context), ...)` 均可替换为 `guard.guard()`，二者公式一致（同一组参数）。

### 3.7.3 射门 / 传球 / 解围 决策树（`player.py` 的 `plan_kick()`）

**原问题**：固定踢球门中心 `(7.25, 0)`、固定力度 5.0 —— 球门中心通常站着对方门将，等于把球送给对手。

```python
# ============ 新增参数（写进 param.py）============
# OWN_DANGER_X          = -4.0    # 球 x 小于它 = 本方危险区
# CLEAR_POWER           = 8.0     # 解围力度
# CLEAR_AHEAD_M         = 3.0     # 解围目标退到门线前多远（前场）
# PRESSURE_DIST_M       = 1.2     # 对手距球多近算「被压迫」
# MAX_SHOT_DIST_M       = 9.0     # 超过此距离不直接射门（太远没威胁）
# GOAL_AIM_FRACTIONS    = (-0.8, -0.4, 0.0, 0.4, 0.8)   # 门内采样（占门半宽比例）
# SHOT_BLOCK_RADIUS_M   = 0.45    # 射线到此距离内有对手 = 被封堵
# SHOT_BLOCK_PENALTY    = 3.0
# SHOT_POWER_BASE       = 2.5
# SHOT_POWER_PER_M      = 0.30
# SHOT_POWER_MIN/MAX    = 3.0 / 8.0
# PASS_POWER_BASE       = 1.5
# PASS_POWER_PER_M      = 0.55
# PASS_POWER_MIN/MAX    = 2.0 / 6.0
# PASS_MIN_GAIN_M       = 1.5     # 队友至少比自己接近球门这么多才值得传
# PASS_LEAD_M           = 0.6     # 传球提前量
# DRIBBLE_POWER         = 3.0     # 无解时朝球门轻推带球

# ============ player.py 新增方法 ============
def _perp_dist(self, px, py, direction, qx, qy) -> float:
    """点 (qx,qy) 到『从 (px,py) 沿 direction 的射线』的垂直距离。"""
    ux, uy = math.cos(direction), math.sin(direction)
    t = (qx - px) * ux + (qy - py) * uy
    return math.hypot(qx - (px + ux * t), qy - (py + uy * t))

def _is_shot_blocked(self, direction) -> bool:
    """射门线上是否有对手挡住。"""
    ball = self.context.ball
    for r in self.context.opponents.values():
        if r.pose is None:
            continue
        t = ((r.pose.x - ball.x) * math.cos(direction)
             + (r.pose.y - ball.y) * math.sin(direction))
        if 0.0 < t and self._perp_dist(ball.x, ball.y, direction,
                                       r.pose.x, r.pose.y) < SHOT_BLOCK_RADIUS_M:
            return True
    return False

def _best_shot_point(self) -> tuple[float, float]:
    """门内采样 5 点，选『离防守者最远 + 未被封堵』的射门点。"""
    ctx = self.context
    ball = ctx.ball
    goal_x = ctx.field.length / 2.0 + GOAL_TARGET_DEPTH_M
    half = ctx.field.goal_width / 2.0
    best, best_score = (goal_x, 0.0), -1e9
    for frac in GOAL_AIM_FRACTIONS:
        y = half * frac
        direction = angle_to(ball.x, ball.y, goal_x, y)
        score = -0.5 * abs(y)                              # 越靠中路容错越大
        for r in ctx.opponents.values():
            if r.pose is None:
                continue
            t = ((r.pose.x - ball.x) * math.cos(direction)
                 + (r.pose.y - ball.y) * math.sin(direction))
            if 0.0 < t and self._perp_dist(ball.x, ball.y, direction,
                                           r.pose.x, r.pose.y) < SHOT_BLOCK_RADIUS_M:
                score -= SHOT_BLOCK_PENALTY
        if score > best_score:
            best_score, best = score, (goal_x, y)
    return best

def _under_pressure(self) -> bool:
    ball = self.context.ball
    return any(r.pose is not None
               and dist(r.pose.x, r.pose.y, ball.x, ball.y) < PRESSURE_DIST_M
               for r in self.context.opponents.values())

def _clearance_direction(self) -> float:
    """解围：踢向前场边路，选对手较少的一侧。"""
    ctx = self.context
    ball = ctx.ball
    up = sum(1 for r in ctx.opponents.values() if r.pose and r.pose.y > 0)
    dn = sum(1 for r in ctx.opponents.values() if r.pose and r.pose.y <= 0)
    side = 1.0 if up <= dn else -1.0
    tx = ctx.field.length / 2.0 - CLEAR_AHEAD_M
    ty = side * (ctx.field.width / 2.0 - 1.0)
    return angle_to(ball.x, ball.y, tx, ty)

def _best_pass_target(self):
    """找『明显更靠近对方球门』的队友，返回带提前量的接球点；没有则 None。"""
    ctx = self.context
    ball = ctx.ball
    goal = opponent_goal(ctx)
    my_d = dist(ball.x, ball.y, goal[0], goal[1])
    best, best_gain = None, PASS_MIN_GAIN_M
    for pid, r in ctx.teammates.items():
        if pid == self.id or r.pose is None:
            continue
        gain = my_d - dist(r.pose.x, r.pose.y, goal[0], goal[1])
        if gain > best_gain:
            a = angle_to(r.pose.x, r.pose.y, goal[0], goal[1])
            best = (r.pose.x + PASS_LEAD_M * math.cos(a),     # 提前量：传到队友身前
                    r.pose.y + PASS_LEAD_M * math.sin(a))
            best_gain = gain
    return best

# ============ player.py —— 替换 plan_kick() ============
def plan_kick(self) -> tuple[float, float] | None:
    """决策树：解围 > 射门 > 传球 > 带球推进。返回 (direction, power)。"""
    ctx = self.context
    ball = ctx.ball if ctx is not None else None
    if ctx is None or ball is None:
        return None

    goal = opponent_goal(ctx)
    d_goal = dist(ball.x, ball.y, goal[0], goal[1])

    # ① 本方危险区且被压迫 → 大脚解围到前场边路
    if ball.x < OWN_DANGER_X and self._under_pressure():
        return self._clearance_direction(), CLEAR_POWER

    # ② 有射门机会 → 射向最优门内点，力度随距离
    aim = self._best_shot_point()
    direction = angle_to(ball.x, ball.y, aim[0], aim[1])
    if (d_goal <= MAX_SHOT_DIST_M
            and not self._is_shot_blocked(direction)
            and self.kick_can_score(direction)):
        power = clamp(SHOT_POWER_BASE + SHOT_POWER_PER_M * d_goal,
                      SHOT_POWER_MIN, SHOT_POWER_MAX)
        self._draw_kick_target(aim)
        return direction, power

    # ③ 射门被封 → 传给位置更好的队友
    pt = self._best_pass_target()
    if pt is not None:
        d_pass = dist(ball.x, ball.y, pt[0], pt[1])
        power = clamp(PASS_POWER_BASE + PASS_POWER_PER_M * d_pass,
                      PASS_POWER_MIN, PASS_POWER_MAX)
        self._draw_kick_target(pt)
        return angle_to(ball.x, ball.y, pt[0], pt[1]), power

    # ④ 都没戏 → 朝对方球门轻推，继续带球
    return angle_to(ball.x, ball.y, goal[0], goal[1]), DRIBBLE_POWER
```

### 3.7.4 分区对 NORMAL 的修正（第 4 章会细化）

`_act_normal` 里已内建两处分区判断：
- `OWN_DANGER_X = -4.0`（**Z0**）→ 触发门将出击 + 解围力度；
- `SUPPORT_ATTACK_MIN_X = -1.5`（**Z2 边界**）→ 决定 Support 是补防还是前插。

更细的比分/时间自适应见第 4 章。

### 验收
- ✅ 球在对方半场且无人封堵时，Attacker **射门且能打在门框内**（用 `kick_can_score` 校验）。
- ✅ 射门被封堵时，会选择传给更靠前的队友（可视化能看到踢球目标 X 标记移到队友身上）。
- ✅ 球在本方禁区被围抢时，会**大脚解围到前场边路**（不是横传）。
- ✅ 门将随球的 y 左右滑动，球打到边路时门将不在中间空门。
- ✅ 三名球员**不会挤成一团**（任意两人间距 > 1.5m）。

---

# 第 4 章　场景自适应：比分 / 时间 / 人数 / 分区

> 原则：**不新增顶层 Phase**，全部在已有 Phase（主要是 `NORMAL`）内部加判断。这是高分队伍的通用做法。

## 4.1 比分自适应

### 读取方式
```python
game = context.game
my_team  = game.get_team_state(context.team_id) if game else None
opp_team = next((t for t in game.teams if t.team_number != context.team_id), None) if game else None
score_diff = (my_team.score - opp_team.score) if (my_team and opp_team) else 0
```

### 策略表

| 局面 | 判据 | 阵型调整 | 代码改法 |
| :--- | :--- | :--- | :--- |
| **落后** | `score_diff <= -1` | 压上：Support 推到中场当第二进攻点，用防守风险换进球 | `SUPPORT_ATTACK_MIN_X` 临时降到 **-3.5**；Support 强制进攻位 |
| **领先** | `score_diff >= 1` | 收缩：Support 回撤成第二后卫，宁可不进也不丢 | `SUPPORT_ATTACK_MIN_X` 提到 **0.0**；Support 强制补防位 |
| **大比分领先** | `score_diff >= 2` | 死守：Attacker 也优先解围，不追求配合 | 领先分支 + `CLEAR_POWER` 提至 8.5，射门阈值 `MAX_SHOT_DIST_M` 降到 6.0 |
| **平局** | `score_diff == 0` | 标准 1-1-1 | 用默认参数 |

### 代码插入点（`_act_normal` 开头）
```python
# 【新增参数】PRESS_SCORE_DIFF = -1     # 落后阈值
# 【新增参数】PARK_SCORE_DIFF  = 1      # 领先阈值
# 【新增参数】ATTACK_MIN_X_BEHIND = -3.5
# 【新增参数】ATTACK_MIN_X_AHEAD  = 0.0

def _attack_min_x(context) -> float:
    """Support 前插门槛：落后时降低（敢压上），领先时提高（收缩）。"""
    game = context.game
    if game is None:
        return SUPPORT_ATTACK_MIN_X
    my  = game.get_team_state(context.team_id)
    opp = next((t for t in game.teams if t.team_number != context.team_id), None)
    if my is None or opp is None:
        return SUPPORT_ATTACK_MIN_X
    diff = my.score - opp.score
    if diff <= PRESS_SCORE_DIFF:
        return ATTACK_MIN_X_BEHIND      # 落后 → 敢压上
    if diff >= PARK_SCORE_DIFF:
        return ATTACK_MIN_X_AHEAD       # 领先 → 收缩
    return SUPPORT_ATTACK_MIN_X
```
然后把 `_support_point()` 里的 `SUPPORT_ATTACK_MIN_X` 换成 `_attack_min_x(context)`。

## 4.2 时间自适应（终场博弈）

### 读取方式
```python
secs_left = context.game.secs_remaining if context.game else 9999
```

### 策略表

| 局面 | 判据 | 打法 |
| :--- | :--- | :--- |
| **终场领先·死守** | `secs_left <= 30 且 diff >= 1` | 全员回收本方半场；球到脚下就**大脚解围**到前场边路；Support 只补防不前插 |
| **终场落后·搏命** | `secs_left <= 30 且 diff <= -1` | Support 推到对方禁区边缘；甚至 Guard 前压到中线（保留最后 10s 才回收） |
| **终场平局·稳** | `secs_left <= 30 且 diff == 0` | 维持标准站位，不冒险，优先不丢球 |
| **开场** | `secs_left > 常规时长 - 60` | 标准站位，观察对手打法（可记录对手习惯，进阶） |

### 代码要点
```python
# 【新增参数】ENDGAME_SECS = 30
def _endgame_mode(context) -> str:
    game = context.game
    if game is None or game.secs_remaining > ENDGAME_SECS:
        return "normal"
    my  = game.get_team_state(context.team_id)
    opp = next((t for t in game.teams if t.team_number != context.team_id), None)
    if my is None or opp is None:
        return "normal"
    diff = my.score - opp.score
    if diff >= 1:  return "park"      # 死守
    if diff <= -1: return "allin"     # 搏命
    return "steady"
```
在 `_act_normal` 里读 `_endgame_mode(context)`：
- `"park"` → Support 强制补防位 + `CLEAR_POWER=8.5` + 门将 `GUARD_LINE_OFFSET_M` 调到 0.4（贴门线）
- `"allin"` → Support 站到 `(HL-2.0, ±1.5)` 对方禁区边缘
- 其余用默认

> **同样的比分，剩 5 分钟和剩 10 秒的最优解完全不同** —— 时间维度必须单独判断。

## 4.3 人数自适应（有人被罚下）

`play()` 里的 `active` 列表已自动过滤被罚下/摔倒/无位姿的球员，角色分派天然适配。但要保证**人数不足时不崩**：

| 可用人数 | 阵型 | 说明 |
| :--- | :--- | :--- |
| 3 | Guard + Attacker + Support | 完整 1-1-1 |
| 2 | Guard + Attacker | 无 Support；Attacker 兼顾进攻与回追 |
| 1 | 只 Guard | 全员被罚，唯一一人守门（不要去追球） |

**代码已在 3.7.1 处理**（`if not rest:` 分支）。验收时要专门测「只剩 1 人」的极端情况不报 `IndexError`。

## 4.4 分区策略细化（Zone 完整表）

| Zone | 判据 | Attacker | Guard | Support |
| :--- | :--- | :--- | :--- | :--- |
| **Z0** `x < -4.0` | 本方禁区危急 | 优先**解围**（不组织） | 出击或贴门线（视谁更近） | 强制补防位，退到球与门之间 |
| **Z1** `-4.0 ≤ x < -1.5` | 本方后场 | 稳健处理，能传就传 | 门前 0.6m 滑动 | 补防位 |
| **Z2** `-1.5 ≤ x ≤ 1.5` | 中场 | 标准（射/传/带） | 门前 0.6m 滑动 | 按球权切换 |
| **Z3** `1.5 < x ≤ 4.0` | 对方前场 | 敢射（距离 ≤9m） | 可前压到 `x=-1.0` | 进攻接应位 |
| **Z4** `x > 4.0` | 对方禁区 | **必射**（最优先） | 前压到 `x=0.0` 附近 | 进攻接应位，抢二点 |

---

# 第 5 章　参数总表（`param.py` 新增清单）

**AI 执行前先把下面所有参数写进 `param.py`**，`main.py` / `player.py` 通过 `from .param import *` 自动可见。

## 5.1 站位与合法化

| 参数 | 建议值 | 含义 |
| :--- | :--- | :--- |
| `LEGAL_MARGIN_M` | 0.3 | 合法化余量（离边界/中线/中圈的最小距离） |
| `READY_TAKER_BACK_M` | 0.3 | READY 我方开球：主罚者退到中圈后多远 |
| `READY_SUPPORT_X` / `READY_SUPPORT_Y` | -3.0 / 2.0 | READY 接应点 |
| `READY_BLOCK_BACK_M` / `READY_BLOCK_Y` | 0.8 / 1.4 | READY 对方开球：封堵点 |
| `OPP_KICKOFF_MARK_DIST_M` | 1.2 | 盯人时站对手身后（靠我门侧）多远 |

## 5.2 门将

| 参数 | 建议值 | 含义 |
| :--- | :--- | :--- |
| `GUARD_LINE_OFFSET_M` | 0.6 | 站门线前多远 |
| `GUARD_Y_GAIN` | 0.6 | y 跟随球的比例（<1 防被晃） |
| `GUARD_Y_LIMIT_M` | 1.0 | 门将横向活动范围 |

## 5.3 开球

| 参数 | 建议值 | 含义 |
| :--- | :--- | :--- |
| `KICKOFF_PASS_POWER` | 3.0 | 开球短传力度（原 5.0 太大） |
| `KICKOFF_SUPPORT_X` / `KICKOFF_SUPPORT_Y` | -1.0 / 2.5 | 开球接应点 |
| `KICKOFF_STAGE_M` | **1.2**（原 2.0） | 主罚者绕后距离 |

## 5.4 定位球

| 参数 | 建议值 | 含义 |
| :--- | :--- | :--- |
| `OPP_SET_AVOID_M` | **1.65** | 对方定位球避让距离（规则 1.45，留余量） |
| `WALL_SPACING_M` | 0.6 | 人墙横向间距 |
| `SETPLAY_CHASE_BEHIND_M` | 0.35 | 主罚者绕到球后多远 |
| `SETPLAY_PASS_POWER` | 4.5 | 定位球传球基准力度 |
| `SETPLAY_CLEAR_POWER` | 7.5 | 后场解围力度 |
| `CORNER_AIM_X_BACK_M` / `CORNER_AIM_Y` | 1.5 / 0.8 | 角球传中落点 |
| `CORNER_SECOND_X_BACK_M` | 3.0 | 角球二点（弧顶）埋伏点 |
| `GK_OUT_X` / `GK_OUT_Y` | -2.0 / 3.5 | 球门球安全出球点 |
| `THROW_IN_SAFE_X` | 2.5 | 界外球向前推进距离 |

## 5.5 射门 / 传球 / 解围

| 参数 | 建议值 | 含义 |
| :--- | :--- | :--- |
| `OWN_DANGER_X` | -4.0 | 本方危险区 x 阈值（Z0 边界） |
| `CLEAR_POWER` | 8.0 | 解围力度 |
| `CLEAR_AHEAD_M` | 3.0 | 解围目标退到门线前多远 |
| `PRESSURE_DIST_M` | 1.2 | 对手距球多近算「被压迫」 |
| `MAX_SHOT_DIST_M` | 9.0 | 超此距离不直接射门 |
| `GOAL_AIM_FRACTIONS` | `(-0.8,-0.4,0,0.4,0.8)` | 门内采样点（占门半宽比例） |
| `SHOT_BLOCK_RADIUS_M` | 0.45 | 射门线上此距离内有对手 = 封堵 |
| `SHOT_BLOCK_PENALTY` | 3.0 | 封堵扣分 |
| `SHOT_POWER_BASE` / `PER_M` | 2.5 / 0.30 | 射门力度 = base + per_m × 距离 |
| `SHOT_POWER_MIN` / `MAX` | 3.0 / 8.0 | 射门力度夹取 |
| `PASS_POWER_BASE` / `PER_M` | 1.5 / 0.55 | 传球力度公式 |
| `PASS_POWER_MIN` / `MAX` | 2.0 / 6.0 | 传球力度夹取 |
| `PASS_MIN_GAIN_M` | 1.5 | 队友至少比自己接近球门多少才传 |
| `PASS_LEAD_M` | 0.6 | 传球提前量 |
| `DRIBBLE_POWER` | 3.0 | 无解时轻推带球力度 |

## 5.6 场景自适应

| 参数 | 建议值 | 含义 |
| :--- | :--- | :--- |
| `POSSESSION_MARGIN_M` | 0.2 | 控球判据余量 |
| `SUPPORT_ATTACK_MIN_X` | -1.5 | Support 前插门槛（球 x 需超过） |
| `SUPPORT_ATTACK_AHEAD_M` | 2.5 | 接应点在 Attacker 前方多远 |
| `SUPPORT_ATTACK_SIDE_M` | 1.5 | 接应点横向拉开 |
| `PRESS_SCORE_DIFF` / `PARK_SCORE_DIFF` | -1 / 1 | 落后 / 领先阈值 |
| `ATTACK_MIN_X_BEHIND` / `AHEAD` | -3.5 / 0.0 | 落后压上 / 领先收缩时的前插门槛 |
| `ENDGAME_SECS` | 30 | 终场博弈时间窗 |

## 5.7 优先调的 6 个旋钮（按影响力排序）

1. `MAX_LINEAR`（现 2.0）—— 跑得快不快，最直接，过大易摔倒
2. `KICK_POWER_*` / `SHOT_POWER_*` —— 射门力度，太软够不着门，太猛易踢飞
3. `OPP_SET_AVOID_M` —— **合规生死线**，绝不低于 1.6
4. `SUPPORT_DIST_M`（现 3.0）—— 阵型松紧
5. `GUARD_Y_GAIN` / `GUARD_LINE_OFFSET_M` —— 门将站位，直接决定丢球数
6. `KICK_ENTER_M` / `KICK_EXIT_M`（2.0 / 2.5）—— 追球迟滞，决定进攻稳定性

> **调参铁律**：**一次只改一个**，改完跑完整场对比，记录结果。同时改三个等于没改。

---

# 第 6 章　实施路线图（AI 按此顺序落地）

| 优先级 | 任务 | 改哪个文件 | 对应章节 | 验收 |
| :--- | :--- | :--- | :--- | :--- |
| 🔴 **P0** | **修复 `OPP_SET_PLAY` 抢球 Bug** | `main.py` | 3.6 | 对方定位球全程无人被罚下，全员距球 ≥1.6m |
| 🔴 **P0** | 加公共工具：`_clamp_legal` / `_push_away_from_ball` / `_kick_towards` / `_select_guard` / `_less_crowded_side` / `_mark_point` / `_wall_points` | `main.py` | 3.0.1 | 无语法错误，单测站位点在界内 |
| 🟠 **P1** | ` param.py` 全量新增参数 | `param.py` | 第 5 章 | 无 `NameError` |
| 🟠 **P1** | 重写 `_act_ready`（角色分配 + 合法化） | `main.py` | 3.2 | 有人被罚下时不崩；进 SET 无违例 |
| 🟠 **P1** | 重写 `_act_opp_kickoff`（盯人 + 合法化） | `main.py` | 3.4 | 无人进中圈；第三人有站位 |
| 🟡 **P2** | 重写 `_act_our_kickoff`（二过一 + 球已动转 normal） | `main.py` | 3.3 | 开球不直接射门；10s 内球出中圈 |
| 🟡 **P2** | 改造 `player.guard()`（门线滑动） | `player.py` | 3.7.2 | 门将随球 y 滑动 |
| 🟡 **P2** | 重写 `_act_normal`（先定门将再选 attacker + support 分球权） | `main.py` | 3.7.1 | 三人不成团；门将不被拉去进攻 |
| 🟢 **P3** | 改造 `plan_kick()`（射/传/解围决策树 + 新方法） | `player.py` | 3.7.3 | 被封堵会传球；危险区会解围 |
| 🟢 **P3** | 重写 `_act_our_set_play`（四种剧本） | `main.py` | 3.5 | 角球能传到门前；球门球不横传 |
| 🔵 **P4** | 比分 / 时间自适应 | `main.py` | 第 4 章 | 落后会压上，领先会收缩 |
| 🔵 **P4** | 终场博弈（park / allin） | `main.py` | 4.2 | 最后 30s 领先时全员回收 |

> **AI 执行建议**：**一个 P 阶段做完就跑一次仿真验证**，不要把 P0~P4 全写完再测 —— 否则出问题时无法定位是哪一步引入的。

---

# 第 7 章　测试与验收清单

## 7.1 合规验收（一票否决）

| # | 检查项 | 方法 | 通过标准 |
| :--- | :--- | :--- | :--- |
| 1 | SET / STOPPED 阶段不动 | 看 SET 期间机器人位置 | 1s 内漂移 < 0.05m，无 `MOTION_IN_SET` |
| 2 | 对方定位球退够距离 | 看可视化 `dist` 标签 | 我方**每人**距球 ≥ 1.6m |
| 3 | 对方定位球不碰球 | 看是否有 `[KICK]` 标记 | 主罚前我方无人进入踢球状态 |
| 4 | 对方开球不进中圈 | 看我方 x 坐标 | 全程 `x < 0` 且 `dist((0,0)) ≥ 1.8` |
| 5 | READY 站位合法 | 进 SET 瞬间记录位置 | 无 `ILLEGAL_POSITIONING` |

## 7.2 功能验收

| # | 检查项 | 通过标准 |
| :--- | :--- | :--- |
| 6 | 开球二过一 | 开球后球先到接应者，10s 内出中圈 |
| 7 | 角球战术 | 球传到对方门前 3m 内，有队友在抢点区 |
| 8 | 球门球安全 | 从不横传中路被断 |
| 9 | 射门决策 | 无人封堵时打门框内；被封堵时传球 |
| 10 | 解围 | 本方禁区被围抢时踢向前场边路 |
| 11 | 门将滑动 | 球到边路时门将 y 跟随，不站死中间 |
| 12 | 不成团 | 任意两名我方球员间距 > 1.5m |
| 13 | 人数不足 | 只剩 1~2 人时不崩、不越界 |

## 7.3 仿真测试方法

- **每次只改一个变量**，改前备份，改后跑完整场（10 分钟）再评估。
- **重复多次**：仿真与现实都有不确定性，同一套策略至少跑 3~5 场看趋势，别用一场结果下结论。
- **记录表**：每场记录 `进球数 / 失球数 / 被罚下次数 / 球权保有率 / 定位球成功率`，横向对比。
- **看调试可视化**：`phase/state/set/secondary` 标签确认阶段判定正确；`[KICK]` 标记确认踢球时机；黄色箭头确认走位朝向。

---

# 附录 A　常见坑 Top 10

| # | 现象 | 原因 | 排查 |
| :--- | :--- | :--- | :--- |
| 1 | **对方定位球被连续罚下** | `_act_opp_set_play` 调了 `_act_normal` 去抢球 | 改 3.6；确认本 Phase 内无 `attack()` |
| 2 | **进球后有人被罚下** | READY 站位进中圈/对方半场 | 所有站位点过 `_clamp_legal(own_half=True, out_circle=True)` |
| 3 | **主罚者绕着球打转，永远踢不到** | 给主罚者开了 `avoid_ball=True` | 主罚者必须 `avoid_ball=False` |
| 4 | **机器人走位时不朝向目标** | `face` 在远距离行走时被忽略（2.2.2） | 用目标点决定朝向，或先 `face_to()` 再 `walk_to()` |
| 5 | **角色每帧抖动，机器人原地抽搐** | 没用迟滞 | 用 `ATTACKER_KEEP_DIST_MARGIN_M` + `store` 锁定（2.6） |
| 6 | **两个机器人抢同一个位置/互相挡路** | 站位没考虑队友 | 站位加横向偏移（`SUPPORT_ATTACK_SIDE_M`），开启 `avoid_robots=True` |
| 7 | **踢球后自己摔倒** | 力度过大（>8~10） | `SHOT_POWER_MAX` 压到 8.0 |
| 8 | **球在自家门口出不去** | 只会朝球门方向踢，被断后丢球 | 启用解围分支（`ball.x < OWN_DANGER_X`） |
| 9 | **门将跑去进攻，球门空虚** | 先选 attacker 再选 guard | 改为**先定门将**，attacker 从剩下的人里选（3.7.1） |
| 10 | **有人被罚下后报错 / 有人没站位** | 用 `players[0]` 下标分配、或 `zip` 截断 | 改角色分配 + 人数分支判断 |

---

# 附录 B　公式速查

```python
# 沿朝向走 d 米 → 目标点
target = (px + d*cos(theta), py + d*sin(theta))

# 掉头 180°
back = normalize_angle(theta + pi)

# 沿某方向走多远才能到达目标（投影法）
n = (tx - px)*cos(dir) + (ty - py)*sin(dir)

# 球后方站位（球在人与目标之间）
_behind_ball(bx, by, aim, offset)  →  (bx - ux*offset, by - uy*offset)
#   其中 (ux,uy) 是「球 → aim」的单位向量

# 点到射线的垂直距离
t = (qx-px)*cos(dir) + (qy-py)*sin(dir)
perp = hypot(qx - (px + t*cos(dir)), qy - (py + t*sin(dir)))

# 合法化：夹进场地 + 本方半场 + 中圈外
_clamp_legal(ctx, pt, own_half=True, out_circle=True)

# 避让：推到距球 >= avoid
_push_away_from_ball(ctx, pt, avoid=1.65)

# 踢球：direction 是场地绝对角，不用管机器人朝向
p.kick(angle_to(ball.x, ball.y, tx, ty), power)
```

---

# 附录 C　战术速查卡（贴在墙上的那种）

```
┌─────────────────────────────────────────────┐
│  防守优先 · 三角站位 · 死球是白送的分数        │
├─────────────────────────────────────────────┤
│ STOPPED    站定不动（动=罚下）                │
│ READY      走阵型，为每个 SET 合法做准备      │
│ OUR_KICKOFF  二过一轻传（直接射门进球无效！） │
│ OPP_KICKOFF  退本方半场+中圈外，盯人堵线路    │
│ OUR_SET_PLAY 按类型写剧本（角球传中+二点）    │
│ OPP_SET_PLAY 退 1.65m 布防（碰球=罚下！）     │
│ NORMAL     1-1-1：逼抢 / 门将滑动 / 接应     │
│            Z0 解围 · Z2 标准 · Z4 压上       │
│            落后压上 · 领先收缩 · 终场分情况   │
├─────────────────────────────────────────────┤
│ 🔴 红线：SET 不动 / 对方定位球退 1.65m 不碰球 │
└─────────────────────────────────────────────┘
```

---

**文档结束。** 任何修改都应回到第 0 章铁律做一次合规自检，再照第 7 章清单验收。

