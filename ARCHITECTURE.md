# AI-Native Learning — 分层架构 v0.2

## 定位

**不是 Canvas 的替代品，是 Canvas 的收件箱兼代理人。**

学生不需要学校批准就能用；代理替他穿过 Canvas + 4~5 个第三方站点，
把「找东西」消灭掉，只剩「做东西」和「学东西」。

## 产品论点（用户原话提炼）

> 中国把一切集成进 App，点几下就完事；美国保留了大量人工环节，官僚但有人情味
> —— 因为你能跟人真切地交流，而不是对着一个找不到的按钮感到沮丧。

两种模式各有一个失败模式：

| | 集成（中国式） | 人工（美国式） |
|---|---|---|
| 优势 | 效率高、步骤少 | 可追问、有解释、能兜底 |
| 失败模式 | **一旦出错就无处申诉**，系统不解释自己 | **摩擦巨大**，找不到入口时无人可问 |

**LLM 层的独特价值正在于同时提供两者**：底下是集成（自动同步、自动提醒、自动投递），
上面是对话（可以随时问"为什么"、"在哪"、"这是什么意思"）。
这是本项目存在的真正理由，不是"又一个 LMS"。

---

## 八层

```
 L7  执行层 Act        代交作业 / 深链直达            ← 出口
 L6  对话层 Converse   Telegram 单 bot + 话题路由      ← 唯一接触面
 L5  调度层 Schedule   早 8 点简报 / 死线升级 / 计划推进
 L4  规划层 Plan       LLM 把大作业拆成多天子任务
 L3  知识层 Knowledge  PPT/PDF 入库 + 检索 = 每科私教
 L2  路由层 Route      每门课的"东西到底在哪"映射表
 L1  规范层 Normalize  统一 schema (SQLite)
 L0  采集层 Ingest     API ‖ Scrape 双通道            ← 入口
```

---

### L0 采集层 — 双通道，可互换

用户判断正确：**不能把命脉押在 Canvas API 上**。不是因为 Instructure 会封杀
（Canvas 是 AGPL 开源的，API 有公开文档），而是因为
**学校管理员可以逐个关闭能力** —— 已实测：本账号的 `/api/v1/users/self/page_views`
返回 **403 unauthorized**，就是 UW 管理员关掉的。

所以两条通道从第一天就并存，藏在同一个接口后面：

```python
class CanvasSource(Protocol):
    def courses(self) -> list[Course]: ...
    def assignments(self, course_id) -> list[Item]: ...
    def materials(self, course_id) -> list[Material]: ...

class ApiSource(CanvasSource):     # 快、稳、结构化。首选
class ScrapeSource(CanvasSource):  # 慢、脆、但没人能关掉。兜底
```

上层永远不知道数据是怎么来的。API 被关 → 换实现，零改动。

**Scrape 通道的工具选型：**

| 需求 | 选择 | 理由 |
|---|---|---|
| 浏览器驱动 | **Playwright** (Python) | 唯一原生支持 `persistent_context` 的，登录态能跨重启保留 |
| 登录态 | `launch_persistent_context(user_data_dir=...)` | 一次人工登录 SSO，之后长期复用，不碰密码 |
| 跨站点 | 每个站点一个 **adapter 类** | Canvas / Gradescope / McGraw Hill 各写各的，共用同一个 browser context |
| 反漂移 | 语义选择器 (`get_by_role`) 而非 CSS 路径 | 对方改版时存活率高一个数量级 |

**跨站点怎么保持"它们知道你是谁"**：所有 adapter 共享**同一个** persistent context。
你在 Mac mini 上人工登录一次 Canvas、一次 Gradescope，cookie 就都在那个 profile 里了，
之后所有脚本共用。这也是为什么必须常驻本地——登录态不能上云。

### L1 规范层 — 统一 schema

四门课的数据结构天差地别（MATH222 全是 external_tool，PHYSICS247 只有 2 个 item），
必须先拍平成一张表，上层才有可能通用。

```
course(id, code, name, term, source)
item(id, course_id, kind, title, due_at, url, submit_tier, external_host)
material(id, course_id, title, mime, local_path, sha256, fetched_at)
route(course_id, yaml)
task(id, item_id, title, planned_for, status)      -- L4 产物
event(id, kind, payload, at)                        -- 审计
```

关键字段 `submit_tier` ∈ {A, B, C}，直接决定 L7 走哪条路。

### L2 路由层 — 项目的核心资产

每门课的混乱是**教授个人习惯**造成的，不是 Canvas 造成的，
而这些规则**只存在于 syllabus 和第一节课的口头说明里，API 里查不到**。

来源：syllabus PDF + 第一节课录音转写 → LLM 填 YAML → 人工校对一次 → 长期使用。
格式见 `routes/*.yaml`。

### L3 知识层 — 每科一个私教

- 老师的 PPT / 讲义 / 课本往话题里一丢，自动入库、切块、向量化
- Canvas 上的材料由 L0 自动抓来入库，不用人工下载
- 每门课一个独立 corpus → **这就是"每个 bot 有自己的 context window"的真实实现**
  （不需要真的开多个 bot，per-topic 检索范围隔离即可）
- 支持预习（"这节课要讲什么，我需要先懂什么"）和复习（"今天讲的三个要点"）

> **边界**：L3 可以讲解、可以出练习题、可以判断你哪里没懂并主动来问你。
> 但它**不替你写计分作业的答案**，L7 也**不提交你没做过的东西**。
> 这条线一旦破了，这个项目就不可能交给学校。

### L4 规划层 — 解决"任务不一定是一天的"

用户指出的真问题：有些作业给你一周时间，日历上却只有一个截止点。

```
Item(due 7 days out, 估计工作量 6h)
   └─ LLM 拆解 ─► Task(D-6: 读完 ch.3)
                  Task(D-4: 做 1-10 题)
                  Task(D-2: 做 11-20 题 + 检查)
                  Task(D-1: 上传)
```

拆解结果写进 `task` 表，由 L5 按日推送。用户可在话题里直接改（"第二步挪到周末"）。

### L5 调度层

- **每天 08:00** — 简报：今天要交什么、计划里今天该推进什么、有什么新材料
- **死线升级** — D-3 / D-1 / D-0 三档，按课分开发到各自话题
- **计划推进** — L4 拆出来的子任务到点提醒
- **变更侦测** — 轮询 L0，发现新作业/材料/改期就立刻推送（这是替代"微信群老师发一句话"的关键）

实现：APScheduler（Python），持久化 job store 存 SQLite，Mac mini 重启不丢。

### L6 对话层 — 一个 bot，话题路由

**决定：单 bot + Forum Topics，不是多 bot。**

Telegram 的硬约束：
1. bot 在群里默认开 **privacy mode**，只收得到 @它 / 回复它 / `/命令` 的消息
2. **bot 之间互相收不到消息**，多 bot 协作在 Telegram 上不成立
3. 关掉 privacy mode 会让每个 bot 收到群里所有消息，更混乱

正确形态：

```
超级群「我的课」（开启 Topics）
 ├─ 📌 总览        ← 每天 08:00 简报发这里
 ├─ MATH 222      ← 该话题内所有消息自动进 MATH222 上下文
 ├─ PHYSICS 247
 ├─ SPANISH 101
 └─ ESL 116
```

bot 从 `message.message_thread_id` 判断课程，**你直接说话就行，不用 @，不用切换按钮**。
每个话题维护独立会话历史 + 独立检索范围 = 用户想要的"不同 bot 有不同 context window"。

代价：四科共用一个 bot 头像和名字（纯视觉损失）。

**文件大小限制（重要，影响 L3 推课本）**：
标准 Bot API 上传上限 **50 MB**、下载上限 20 MB。
想要 2 GB 必须在 Mac mini 上自建 **local Bot API server**（官方支持，开源）。
课本 PDF 常超 50 MB，所以这一步基本是必须的，要排进里程碑。

### L7 执行层 — 三档降级

| 档 | 适用 | 机制 | 现状 |
|---|---|---|---|
| **A 全自动** | Canvas 原生 `online_upload` / `online_text_entry` / `online_url` / 讨论 | 官方 REST API | ✅ 已上线 |
| **B 浏览器代录** | **WebAssign（MATH222）**；Gradescope（PHYSICS247） | 在学生自己的登录态里填入**学生自己做的**答案，渲染结果回传聊天，**学生点确认后**才提交 | WebAssign ✅；Gradescope 未做 |
| **C 一键直达** | McGraw Hill（SPANISH101）等还没接的平台 | 只给深链 | ✅ |

即使只做到 C 档也去掉了约 80% 的痛，因为痛点从来不是"点提交"，是"找到在哪提交"。

> **2026-09-19 修订。** 初版把 WebAssign 划在 C 档（"计分测评一律不代交"），B 档写的是
> "Gradescope（MATH222 全部）"——后者是事实错误，MATH222 用的是 WebAssign。
> C→B 的改动是学生提出的：如果最后一下 Submit 还得回浏览器按，中间层就又回来了。
> 守住的底线没变——**代理搬运你做出来的东西，不生产、也不提交你没做的东西**：
> 答案是学生手写的，提交前学生看到的是平台真正会判分的渲染结果，没有确认就不提交。
>
> 同一门课里"交到哪"也不是固定的：PHYSICS247 的 PSet 1/2 是 `online_upload`（Canvas 和
> Gradescope **各交一次**），PSet 3 是 `external_tool`（只交 Gradescope，分数自动回传）。
> 这从 `submission_types` 读得出来，见 `src/ainl/external/routing.py`。

---

## 里程碑

| | 内容 | 风险 | 状态（2026-09-20） |
|---|---|---|---|
| **M0** | 记录器收集真实点击路径（证据） | 无 | ✅ 547 条事件、21 个域名 |
| **M1** | L0(API) + L1 + L5 + L6 → 早报和提醒进 Telegram | 无，只读 | ✅ |
| **M2** | L3 材料入库 + 推送（需 local Bot API server） | 低 | 部分：作业附件会推进聊天；课本未做 |
| **M3** | L2 路由表（教授访谈产物） | 低 | 改为**从 Canvas 读**：`routing.py` 按 `submission_types` + 工具 URL 判断，不靠访谈 |
| **M4** | L7-A Canvas 原生提交 | 低 | ✅ 含多文件、讨论 |
| **M5** | L4 规划 + L3 私教问答 | 中 | 未做 |
| **M6** | L7-C 深链直达 | 低 | ✅ |
| **M7** | L7-B 浏览器代录 | 高，需持续维护 | WebAssign ✅（手写→LaTeX→MathML、选择题点选、确认后提交）；Gradescope 未做 |
| **M8** | L0 备用通道（API 被关时的保险） | 中 | ✅ cookie 通道 + 自动降级 + 会话保活 |
| **M9** | OAuth2 化 + 多用户 → 可交付学校 | 政策问题 | 未做。token 已获学校批准（单用户） |

认证现状：**学校批准的 access token 是主通道**，浏览器 cookie 是备用（初版假设相反）。
WebAssign 会话不靠密码，靠 Canvas 的 LTI sessionless launch 重建——对任何有 Canvas 会话的学生都成立。
