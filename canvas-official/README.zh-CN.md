[English](README.md) · **简体中文**

# canvas-official — 官方 Access Token 通道

主通道。用学校批准的 Canvas personal access token，只调用 Instructure 公开文档里的
REST 端点。**不含任何逆向成果**：没有浏览器会话、没有抓来的 CSRF、没有从 dashboard
扒下来的内部 GraphQL 查询。

逆向方案（`../src/ainl/canvas/`，cookie + 内部 GraphQL）降级为**备用通道**，在 token
被撤销、scope 收紧或学校关掉 API 时才用。两条路都实现同一个 `CanvasSource` 语义。

## 为什么单独一个目录

不是为了代码洁癖，是为了**可交付**：这个目录可以单独给学校看、给别的学生用、开源，
不需要连带解释"你们的 Canvas 我是怎么逆向的"。混在一起就没法这样切。

代价记在下面 §Gaps。

## 跑起来

```
setx CANVAS_TOKEN "<token>"
```

（Canvas → Account → Settings → **+ New Access Token**。`setx` 只影响**新**开的窗口。）

```
python -m canvasofficial --probe
```

`--probe` 是第一件该做的事：token 不是全有全无，学校用 scoped developer key 发的
token 会对某些端点回 403 "insufficient scopes"，而 token 本身完全有效。探针把每个端点
都打一遍，打出 PASS / SCOPE / AUTH / 404 / FAIL 表。**这张表决定哪些逆向代码可以退役。**

```
python -m canvasofficial --whoami
python -m canvasofficial --courses
python -m canvasofficial --todo --days 4
python -m canvasofficial --assignment <course_id> <assignment_id>
python -m canvasofficial --submit-files <course_id> <assignment_id> answer.pdf
```

`--submit-files` 会先打印要提交到哪个作业、几个文件多大，要你手输 `submit` 才走。

## 边界

`submit.py` 只提交**学生本人做的**文本或文件到 Canvas 原生作业。**这个包不碰任何第三方平台。**

供应商平台（WebAssign 等）的工作在 `../src/ainl/external/` 里，那边的规则是：
代理搬运你做出来的东西，不生产、也不提交你没做的东西——答案是学生手写的，
提交前学生看到平台真正会判分的渲染结果，**没有确认就不提交**。

## Gaps —— 官方 REST 拿不到的东西

| 能力 | 官方 REST | 备用通道怎么做 |
|---|---|---|
| 跨课程「按提交状态」的作业视图（late / missing / excused 一次拿全） | 只能按课程逐个 `assignments?include[]=submission` | 内部 GraphQL `courseWorkSubmissionsConnection` 一次拿完 |
| 每门课 Due / Missing / Submitted 计数 | 无对应端点 | 内部 GraphQL `submissionStatistics` |
| 供应商平台（WebAssign 等）内部的题目与作答 | 完全不覆盖 | 浏览器里操作学生自己的登录态（`ainl.external.webassign`） |

**已经不是缺口的两项（2026-09-19/20 实测）：**

- **前两行其实能走 token。** `--probe` 带了一个非文档探测 `/api/graphql`，结果是 Bearer token 直接可用
  （见 `PROBE.md`）。它仍是非文档端点，所以留着 REST 逐课路径作慢速等价。
- **LTI launch 有官方端点，本表初版漏了。**
  `GET /api/v1/courses/:id/external_tools/sessionless_launch?assignment_id=…&launch_type=assessment`
  学生权限可用，返回一个一次性 URL，浏览器打开即完成 LTI 启动——不需要自己构造签名，
  也不需要 `target='_self'` 那个技巧。它同时是 Canvas 作业 → 供应商作业的权威映射，
  并且能在没有任何供应商密码的情况下重建供应商会话。返回的 URL 带 `verifier=`，**是凭据，勿入日志**。

另外两个让 `/todo` 从 12.8 秒降到 0.9 秒的官方参数，值得记在这里：
`planner/items` 的 `end_date`，和 `courses/:id/assignments` 的 `assignment_ids[]`——
过滤条件能下推到服务端，就不要留在客户端。

## 与 `ainl` 的关系

`ainl.canvas.ApiSource.from_env()` 已经支持 Bearer：只要 `CANVAS_TOKEN` 存在就自动走
token，不存在才起浏览器。所以设好环境变量之后，早报、Telegram bot、提交全都直接换到
官方通道，无需改动上层。本目录是那条通道的**独立、可审计、可分发**实现。
