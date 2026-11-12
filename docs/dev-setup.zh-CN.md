[English](dev-setup.md) · **简体中文**

# 开发环境与运行

最后核对：2026-09-24。现在托管在 Mac mini 上，Windows 笔记本仍可开发。

**macOS（托管机）**——完整说明见 `docs/macos-hosting.md`：

```bash
cd ~/code/AINATIVELEARNINGOS
/opt/homebrew/bin/python3.12 -m venv .venv && .venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m playwright install chromium
.venv/bin/python -m pytest tests canvas-official/tests scale-up/tests "Cookie Management/tests" -q   # 284 passed
scripts/macos/service.sh install        # 浏览器 + bot 装成 launchd 服务；密钥填 ~/.ainl/env
```

**Windows**：

```powershell
cd D:\code\local\projects\AI-native-learning
python -m pip install -e ".[dev]"
python -m pytest tests canvas-official/tests -q
```

> ⚠️ **不要在 git worktree 或别的副本里再跑 `pip install -e`**。它会把可编辑安装重新指向那个副本，
> 正在运行的 bot 就会悄悄加载旧代码。踩过一次。

## 一、密钥：Windows 放「用户」环境变量，macOS 放 `~/.ainl/env`

```powershell
setx CANVAS_TOKEN        "<Canvas → Account → Settings → New Access Token>"
setx TELEGRAM_BOT_TOKEN  "<@BotFather 给的>"
setx TELEGRAM_CHAT_ID    "<python -m ainl.telegram --discover 打印的>"
setx GEMINI_API_KEY      "<Google AI Studio>"
```

`setx` 只对**新开的**终端生效。macOS 上是 `~/.ainl/env`（chmod 600，`scripts/macos/run.sh` 启动服务前读取）。**密钥永远不进聊天、不进代码、不进日志。**

## 二、Canvas：两条通道

| | token（主） | cookie（备用） |
|---|---|---|
| 来源 | 学校批准的 personal access token | 浏览器里登录好的 Canvas 会话 |
| 需要人在场 | 否 | 首次要 NetID + Duo |
| 失效 | 被撤销为止 | 靠活动续命；后台每 30 分钟保活一次 |
| 能力 | REST + GraphQL 均可（`canvas-official/PROBE.md` 实测 11/13） | 相同 |

`ApiSource.from_env()` 默认 token 优先，失败时降级 cookie，并在 Telegram 里**明说**降级了。
`/channel auto|token|cookie` 可手动钉住；钉 `token` 表示"我想知道 token 还能不能用"，不会降级。

cookie 通道读的是**开着的那个浏览器里的活 cookie**，不是磁盘快照（快照只和上次保存一样新——
踩过一次 46 小时前的快照导致 401）。两边都没有会话时，bot 会把 Canvas 登录页打开等你登。

```bash
python -m ainl.canvas --whoami
python -m ainl.canvas --planner --days 14
python -m canvasofficial --probe          # 这个 token 到底能调哪些端点（在 canvas-official/ 下）
```

## 三、浏览器：无窗口、独立进程

WebAssign 没有 API，要靠一个真浏览器。**它不是任何脚本的子进程**，用调试端口 9222 挂着，
工具连上去、干完活、断开。

```bash
python scripts/browser_start.py             # 已在跑就什么都不做
python scripts/browser_start.py --restart   # 换一个干净的渲染器
python scripts/browser_start.py --show      # 需要人手动登录时才开窗口
```

默认 **headless，屏幕上没有任何窗口**。几条用事故换来的事实：

- **必须 `--disable-gpu`**。否则截图不返回、`document.fonts.ready` 永不 resolve、CDP 断连。
- **窗口最小化 = 打开作业 100% 卡死**（实测 3/3，46 秒）。最小化的窗口不绘制。无窗口模式根除了这个问题。
- **Cengage / WebAssign 的会话 cookie 是会话级的**（`JSESSIONID`、`cmp-session-id`），浏览器一关就没。
  所以**每次重启都会登出 WebAssign**——但不需要密码：代码会用 Canvas 的 LTI 启动自动重建会话（约 10 秒）。
  这一点 `research/hosted-browser-poc/REPORT-2026-09-15.md` 早就记过；后来忘了又踩了一遍。
- **一个进程里 `sync_playwright()` 只能可靠启动一次**，所以驱动是每线程单例（`util/pw.py`），CDP 连接缓存复用、**不要 close**。
- 同一个学生**不要同时开两个浏览器上下文**登 Cengage，会互相踢下线。
- **别在这个无窗口浏览器里开第二个标签页。** `ctx.new_page()` 之后的导航经常永远不提交（2026-09-21 一天三次，
  还会连带卡住驱动连接）。借用现有的那一个标签页，用完导回 WebAssign 列表——`external/gradescope.py` 就是这么做的。

## 四、Telegram

1. **@BotFather** → `/newbot` → 拿 token → 打开 bot 按 **Start**
2. `python -m ainl.telegram --discover` 打印 `TELEGRAM_CHAT_ID`
3. `python -m ainl.telegram --ping` 收到 ✅ 就通了

一个 bot token **只能有一个** `getUpdates` 消费者；起第二个实例两边都会 409。

**对话记录（transcript）**：日志只说"发了一条"，不说发了什么。bot 会把学生和 bot 之间的**每一条**消息——
收到的文字 / 照片 / 文件 / 按钮点击，发出的文字 / 图片 / 相册 / 按钮改动——按一行一个 JSON 追加到
`~/.ainl/transcript.jsonl`（`AINL_HOME` 下）；经手的所有图片（学生拍的答案纸、题图、填好后的截图）
存到 `~/.ainl/photos/`，记录里用相对路径指向它。事后排错就靠它。

```bash
python -m ainl.telegram --transcript          # 最近 40 条：时间 ←/→ 类型 文字(前 300 字) [图片路径] {按钮}
python -m ainl.telegram --transcript 200
python -m ainl.telegram --transcript-since 2026-09-24T23:00     # 本地时间起
AINL_TRANSCRIPT=0 python -m ainl.bot          # 关掉（测试里默认就是关的）
```

写入失败只记一次 warning、绝不影响 bot；记录里**没有任何 token**。
> ⚠️ transcript 和 photos/ 里是学生的作业内容和答案照片，**不要分享、不要进 git、不要贴进聊天**。

## 五、对话 bot

```bash
python -m ainl.bot          # 常驻（重启：Windows scripts/bot_restart.ps1，macOS scripts/macos/service.sh restart bot；都不碰浏览器），长轮询；只服务 TELEGRAM_CHAT_ID 这一个人
```

**入口只有一个：`/todo`。** 其它一切都从它给的编号开始。

```
/todo            今天 → +4 天，每门课一块（A、B、C…）
/todo 14         往后看 14 天（列表下面也有按钮）
回复 B6          打开那一项：
   ✍️ 📎 💬      Canvas 原生作业 —— 发文字 / 文件 / 链接，确认后由官方 API 提交
   🧮            WebAssign —— 发来每道题的截图；选择题点按钮，其余写在一张纸上拍一张照
   ↗             暂时做不了的平台 —— 给链接
/cancel          放弃当前项（连同持有的图片一起释放）
/restart         清掉所有半路状态（bot 没反应时用）
/timing          最近各步骤耗时
/channel         Canvas 走 token 还是 cookie
```

WebAssign 一轮：题图 → 你拍答案纸 → **Gemini 读一次**（整个系统唯一的 LLM 调用）→ 逐题预览
→ `Fill` 填入 → 看渲染后的截图 → `Submit / Modify / Cancel`。
**没有你点确认，绝不提交。** Modify 优先打字改正（0 次模型调用）。

状态存 `~/.ainl/bot_state.json`。图片只在内存里，结束一轮就释放。

## 五·五、`/auto` 与 `/sweep`（公开版不含）

私有仓库里有一个只在开发时打开的开关：让语言模型代替学生作答，用来把填写、提交、批改这条链路完整跑通。
公开版**没有包含这个求解器**，`/auto` 和 `/sweep` 只会回复“不可用”。产品本身从不生成答案：交上去的永远是学生自己写的内容。

## 五·六、提交之后

提交后 bot 逐题报 ✓ / ✗。有 ✗ 时这一轮不结束（stage `graded`）：打字 `2: …`，或再拍一张（整页改过的，或只拍改过的题），
只有和已提交答案**不同**的题会被重新填入、重新提交；分步题（Master It）排在最后，其他题提交后才逐步问。`/cancel` 退出。

## 六、早报

```bash
python -m ainl.brief --preview | --now | --serve     # --serve 每天 08:00（Canvas 时区）
```

## 七、耗时与排错

- 每个浏览器操作有预算，超时由看门狗（`util/browserthread.py`）放弃并重试，必要时自动重启浏览器。
- 逐步耗时：设 `AINL_TRACE_DETAIL=1`，跑 `python scripts/timing_trace.py`，再 `python scripts/timing_report.py`。
- 现状与待办：`docs/optimization-goals.md`；测量结论：`docs/timing-analysis.md`。
- 跑完带 Playwright 的脚本常在**结尾**看到一段 Node `EPIPE` 堆栈——那是被放弃的驱动的退出噪音，
  不是失败。**看输出的开头。**

## 代码结构

```
src/ainl/
  config.py                 环境变量；秘密永不 repr
  canvas/
    api.py                  ApiSource：REST + GraphQL、提交、sessionless_launch
    channel.py              token / cookie 选择与钉住
    session.py              BrowserSession；从活浏览器读 cookie；拉起登录页
    keepalive.py            后台保活 Canvas 会话
    _http.py auth.py models.py source.py
  external/
    webassign.py            列作业 / 题图 / 题型分类 / fill / choose / submit / LTI 自愈
    link.py                 Canvas 作业 ↔ WebAssign 作业（LTI 为准，标题为退路）
    routing.py              这份作业到底交到哪（Canvas / Gradescope / 两边）
    platforms.py            外部平台登记表
  mathinput/                手写 → LaTeX → WebAssign 方言 MathML；可插拔视觉模型
  bot/                      conversation.py（对话）、state.py（持久状态）
  telegram/client.py        send / send_photo / send_media_group / set_commands
  brief/                    早报
  util/                     timing（分类 span）、browserthread（看门狗）、pw（驱动单例）
canvas-official/            只用官方文档化 REST 的独立 token 客户端 + 能力探针
scripts/                    browser_start、timing_*、wa_survey（题型普查）等
tests/                      全部离线，MockTransport / Fake*
```
