# 开机自启动到底启动了什么

最后核对：2026-09-21，逐行对照代码写成（不是凭印象）。配图见同目录 `autoopenexplain.html`。

一句话：**登录 Windows → Startup 文件夹里的快捷方式 → `autostart.ps1` → 先起无窗口 Chromium（端口 9222）→ 再起 `python -m ainl.bot` → bot 问一次 Canvas「我是谁」→ 开始对 Telegram 长轮询。** 到这里为止，bot 进程没有碰过浏览器（token 通道下），没有 Playwright，没有 Gemini。这些全是「第一次用到才启动」。

本文最有用的一条线：**EAGER（启动时就做）** 与 **LAZY（第一次用到才做）** 的区别。每一节都按这个分。

---

## 0. 怎么装上的：登录时运行，不是开机时运行

`scripts/autostart_install.ps1` 做的事只有一件：在当前用户的 Startup 文件夹
（`[Environment]::GetFolderPath('Startup')`，也就是 Win+R → `shell:startup` 打开的那个目录）
里创建快捷方式 `ainl-autostart.lnk`：

| 快捷方式字段 | 值 |
|---|---|
| TargetPath | `powershell.exe`（用 `Get-Command` 解析出的完整路径） |
| Arguments | `-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "<repo>\scripts\autostart.ps1"` |
| WorkingDirectory | 仓库根目录 |
| WindowStyle | `7`（最小化，避免控制台在桌面上闪一下） |

为什么是 **logon** 而不是 **boot**（脚本头部注释原话的意思）：

- 密钥存在 Windows 的 **User** 级环境变量里，浏览器 profile 也属于这个用户；没人登录之前，没有任何有用的东西可以启动。
- 不需要管理员权限、不建计划任务、不改注册表；在资源管理器里看得见、删得掉。

`-Remove` 参数删除这个 `.lnk`，别的什么都不动（不会停掉已经在跑的 bot 和浏览器）。

---

## 1. 时间线（按真实执行顺序）

### 阶段 A：`autostart.ps1`（powershell.exe，隐藏窗口）

1. 建 `~/.ainl/`（若不存在），往 `~/.ainl/autostart.log` **追加**一行 `autostart begin`。
2. **等网络**：循环最多 30 次，每次 `[System.Net.Dns]::GetHostEntry('canvas.wisc.edu')`，失败就睡 4 秒（上限约 2 分钟）。记一行 `network: True/False`。
   原因：刚登录时网络常常还没起来，而 bot 的第一个调用就是 Canvas，失败就退出。
   注意：即使结果是 `False`，脚本**照样往下走**，不会中止。主机名是写死的 `canvas.wisc.edu`，不读 `CANVAS_BASE_URL`。
3. **起浏览器**：`python scripts\browser_start.py`，它的输出逐行以 `browser: ...` 记入日志。随后再轮询 `http://127.0.0.1:9222/json/version` 最多 15 次、每次间隔 1 秒，确认调试端口可用。
4. **起 bot**：调用 `scripts\bot_restart.ps1`，输出以 `bot: ...` 记入日志。
5. 记 `autostart end`，powershell.exe 退出。浏览器和 bot 都**不是**它的存活依赖，会继续运行。

顺序为什么是「先浏览器后 bot」（脚本头注释）：cookie 通道下 bot 启动时要从正在运行的浏览器里读 Canvas cookie，而且所有 WebAssign 操作都要 attach 到它；bot 先起来就会以降级状态启动。

### 阶段 B：`scripts/browser_start.py`（一个短命的 python.exe）

1. `alive(9222)`：GET `http://127.0.0.1:9222/json/version`。已经有浏览器在听 → 打印 `already running ... leaving it alone`，直接返回 0（**幂等**，不会起第二个）。
2. 找 Playwright 自带的 Chromium：`~/AppData/Local/ms-playwright/chromium-*/chrome-win*/chrome.exe`，取排序后的最后一个；找不到就报错退出（提示 `playwright install chromium`）。
3. 用 `subprocess.Popen(..., creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)` **脱离式**启动。浏览器不是这个脚本的子进程：脚本退出、bot 重启、编辑器关闭，都关不掉它。
4. 每秒探测一次端口，最多 20 秒；起来了打印 `browser up on 9222 ...` 返回 0，否则返回 1。

命令行参数与各自存在的理由（都来自代码注释）：

| 参数 | 为什么 |
|---|---|
| `--user-data-dir=~/.ainl/chrome-profile` | 固定 profile：Canvas 登录等持久 cookie 留在这里，重启后还在 |
| `--remote-debugging-port=9222` | 工具通过 CDP attach 的入口 |
| `--remote-debugging-address=127.0.0.1` | 调试端口只绑本机 |
| `--no-proxy-server` | 系统代理曾经弄坏 Canvas 登录 |
| `--no-first-run`、`--no-default-browser-check` | 不弹首次运行/默认浏览器提示 |
| `--disable-gpu` | **关键**。没有它：截图不返回、`document.fonts.ready` 永不 resolve、CDP 中途断连、最后 Playwright 驱动 EPIPE。有它：8 道题 4.8 秒读完 |
| `--disable-dev-shm-usage` | 常规稳定性参数（代码里没有写注释） |
| `--disable-background-timer-throttling` | 后台标签页也必须照常渲染 |
| `--disable-features=CalculateNativeWinOcclusion`、`--disable-backgrounding-occluded-windows`、`--disable-renderer-backgrounding` | Windows 会冻结它认为「被挡住」的窗口的渲染，截图请求就一直等一个不再绘制的渲染器 |
| `--headless=new` | 默认无窗口。看得见的窗口每次启动都会跳到最前，而看门狗会在页面卡死时重启浏览器 —— 等于一次次盖在用户的工作上 |
| `--window-size=1600,1400` | 无窗口模式下的视口大小（截图来自渲染器，不来自屏幕） |
| `--user-agent=...Chrome/...` | 默认 headless UA 会自报 `HeadlessChrome` |
| 最后一个位置参数：`START_URL` | WebAssign 的学生首页 |

`--show` 会去掉最后三项「无窗口」参数，开一个可见窗口 —— 只在需要人手动登录时用。`--restart` 会先结束**只占用这个 profile 的** chrome.exe（按命令行匹配，绝不按进程名杀全部 chrome）。

> **容易忽略的一点（EAGER）**：因为命令行末尾带了 `START_URL`，**浏览器自己在启动瞬间就会去请求 WebAssign 首页**。重启后 WebAssign 的会话 cookie 已经没了，所以这次请求只会落在登出/登录页上，没有任何副作用；但严格说，「启动时联系了哪些网络服务」这张表里 `www.webassign.net` 是有的，发起者是 chrome.exe，不是 bot。

### 阶段 C：`scripts/bot_restart.ps1`

1. **停旧 bot**：`Get-CimInstance Win32_Process -Filter "Name='python.exe'"`，再用 `CommandLine -like '*-m ainl.bot*'` 筛选，只对筛中的 PID 执行 `Stop-Process`。**按完整命令行选，绝不按进程名** —— 按名字停 `python`/`node` 曾经连带干掉过不相干的工具。开机场景下这里通常什么都筛不到。
   原因：同一个 bot token 只允许**一个** `getUpdates` 消费者，两个实例会互相 409。
2. **装载密钥**：对 `CANVAS_TOKEN`、`TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`、`GEMINI_API_KEY`、`CANVAS_BASE_URL`、`AINL_KEEPALIVE_MIN` 这六个名字，逐个 `[Environment]::GetEnvironmentVariable(name, 'User')`，有值就写进当前进程的 `Env:`。值从不打印。
   （从 Startup 快捷方式启动时，powershell.exe 本来就继承了用户环境；这一步的意义在于从一个「`setx` 之前就开着的旧终端」手动重启时也能拿到最新值。）
3. **起新 bot**：`Start-Process python -ArgumentList '-m','ainl.bot' -WorkingDirectory <repo> -WindowStyle Hidden`，stdout 重定向到 `~/.ainl/bot.log`，stderr 重定向到 `~/.ainl/bot.log.err`。
4. 睡 6 秒，数一下命令行匹配的 python.exe 个数，输出 `bot processes: N`，再输出 `bot.log.err` 的最后 2 行（这两行会被 autostart 记进 `autostart.log`）。
5. 这个脚本**从不碰浏览器**。

### 阶段 D：`python -m ainl.bot` → `src/ainl/bot/__main__.py` 的 `main()`

按代码行序：

| # | 调用 | 做什么 | 触网？ |
|---|---|---|---|
| 1 | `logging.basicConfig(...)`；把 `httpx` 日志压到 WARNING | 后者是因为 bot token 在 URL 里，不能让 httpx 把 URL 打进日志 | 否 |
| 2 | `TelegramConfig.from_env()` | 读 `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`；缺任何一个 → `ConfigError` → 退出码 2 | 否 |
| 3 | `ApiSource.from_env(interactive=True)` → `channel.open_source()` | 读 `~/.ainl/channel.json` 的偏好（`auto`/`token`/`cookie`，读不到当 `auto`）。**`auto` 或 `token` 且有 `CANVAS_TOKEN` → 直接用 `BearerAuth` 建 `ApiSource`，到此为止，不碰浏览器、不 import Playwright。** | 否（只是构造对象） |
| 3' | （仅 cookie 通道）`auth_from_live_browser()` → `BrowserSession.probe()` | 见下面「cookie 通道分支」 | 是 |
| 3'' | 第 3 步抛异常时 | 记一条 warning，`channel.set_preference("auto")` **改写** `channel.json`，再试一次。原因：钉死在打不开的通道上会让 bot 起不来，而学生只能在主机上才解得开 | — |
| 4 | `canvas.whoami()` | `GET /api/v1/users/self/profile`，拿 `time_zone`（拿不到用 `UTC`）。这是启动过程中**第一次、也是唯一一次**对 Canvas 的请求。`CanvasAuthError` → 退出码 2 | **Canvas REST** |
| 5 | `BotState.load(~/.ainl/bot_state.json)` | 读回 `offset`（Telegram 更新游标）和每个聊天的 menu / pending / draft / math。文件不存在或损坏 → 空状态 | 否 |
| 6 | `TelegramClient(token)`、`Conversation(canvas, tg, chat_id, state, tz)` | 只建对象。`Conversation.__init__` 里 `self._webassign = None`；`transcribe` 只是一个函数引用 | 否 |
| 7 | `tg.set_commands([...])` | `setMyCommands`：注册 `/todo /restart /cancel /channel /timing /help`。失败只记 warning，不阻止启动 | **Telegram Bot API** |
| 8 | `keepalive.start(base_url, minutes)` | 起 daemon 线程 `canvas-keepalive`。间隔取 `AINL_KEEPALIVE_MIN`，默认 30。**线程第一件事是 `time.sleep(minutes*60)`，所以启动时不发任何请求** | 否 |
| 9 | `log.info("bot up for chat ... (tz ...)")` | 「就绪」标志。经 logging → stderr → `bot.log.err` | 否 |
| 10 | `while True: tg.get_updates(offset=state.offset, timeout=30)` | 长轮询。网络异常：记 warning、睡 5 秒、继续。每处理完一条 update：`state.offset = update_id + 1` → `conv.handle(u)` → `state.save()` | **Telegram Bot API** |

**cookie 通道分支（只有 `channel.json` 钉了 `cookie`，或者没设 `CANVAS_TOKEN` 时才走）**：

- `auth_from_live_browser(base_url)`：`pw.connect("http://127.0.0.1:9222")` —— 这会在 **MainThread** 里第一次 `sync_playwright().start()`（拉起 Playwright 的 node 驱动进程，约 1 秒），再 `connect_over_cdp` attach 到那个 Chromium，从各 context 的 cookie 里挑出 Canvas 的。浏览器没开、或里面没登录 Canvas → 返回 `None`。
- 拿到了就 `sess.probe(live)`：一次 `GET /api/v1/users/self`。200 → 用它。
- 否则 `sess.ensure()`：读磁盘快照 `~/.ainl/<host>.storage_state.json` 再 probe；还不行就尝试交互登录 —— 但此时 headless Chromium 正占着同一个 profile，`_refuse_if_profile_in_use()` 会拒绝，最终包成 `NeedsCanvasLogin` 抛出 → 触发上表第 3'' 步回退到 `auto`。

这也是为什么 `autostart.ps1` 坚持「先浏览器后 bot」。

---

## 2. 清单 (a)：本仓库里哪些部分会运行，按顺序

### EAGER —— 每次登录都会执行

| 顺序 | 文件 | 启动时做什么 |
|---|---|---|
| 0 | `ainl-autostart.lnk`（由 `scripts/autostart_install.ps1` 生成，安装脚本本身开机时不运行） | 被 Windows 在登录时执行 |
| 1 | `scripts/autostart.ps1` | 等 DNS → 起浏览器 → 起 bot → 写 `autostart.log` |
| 2 | `scripts/browser_start.py`（借用 `src/ainl/config.py` 的 `home()`） | 探测 9222；没有就脱离式启动 headless Chromium |
| 3 | `scripts/bot_restart.ps1` | 按命令行停旧 bot；装载 User 环境变量；隐藏启动 `python -m ainl.bot` |
| 4 | `src/ainl/bot/__main__.py` | `main()`，见阶段 D |
| 5 | `src/ainl/config.py` | `TelegramConfig.from_env()`、`CanvasConfig.from_env()`、`home()`（顺带 `mkdir ~/.ainl`） |
| 6 | `src/ainl/canvas/api.py` + `channel.py` + `auth.py` + `_http.py` | 选通道、建 `ApiSource`、`whoami()` |
| 7 | `src/ainl/bot/state.py` | `BotState.load()` |
| 8 | `src/ainl/telegram/client.py` | 建 `httpx.Client`；`set_commands()`；`get_updates()` 循环 |
| 9 | `src/ainl/bot/conversation.py` | 只执行 `__init__`。模块顶层 import 会把 `external/webassign.py`、`mathinput/pipeline.py`、`mathinput/providers/*` 这些**模块**加载进来，但只是定义类和函数，不连浏览器、不调模型 |
| 10 | `src/ainl/canvas/keepalive.py` | `start()` 起线程，然后线程立刻去睡 30 分钟 |

### LAZY —— 第一次用到才发生

| 触发 | 才会发生的事 | 位置 |
|---|---|---|
| 任意一条消息到达 | `Conversation.handle()`；不是 `TELEGRAM_CHAT_ID` 这个聊天的消息直接静默丢弃 | `bot/conversation.py` |
| `/todo` | `canvas.planner(...)`、`assignments_meta_for(...)` —— 纯 Canvas REST。**不开浏览器**：一项作业是不是 WebAssign，是从 Canvas 返回的 external tool URL 判断的（`_doable_vendor`） | `bot/conversation.py`、`canvas/api.py`、`external/platforms.py` |
| 第一次回复一个 🧮（WebAssign）编号 | `_wa()` 才 `WebAssign(launch_url=...)` 建对象 | `bot/conversation.py::_wa` |
| 同上 | `link.dep_for()`：先查 `~/.ainl/lti-map.json`；没命中才走 LTI 启动解析，并把结果写回该文件 | `external/link.py` |
| 第一次浏览器操作 | `browserthread.run()` → `_get()` 创建 `_Worker`，daemon 线程 `browser-1` 才出现 | `util/browserthread.py` |
| `browser-1` 里的第一次 `pw.connect()` | `sync_playwright().start()`：Playwright 的 node 驱动进程才被拉起（约 1 秒，每线程一次），然后 `connect_over_cdp` 到 9222。连接被缓存，**故意不 close** | `util/pw.py` |
| 重启后第一次 WebAssign 操作 | `_webassign_tab()`：没有已登录的 WebAssign 标签 → 打开列表页仍是登出 → 调 `launch_url()`（Canvas REST 的 sessionless launch）→ 浏览器 `goto` 这个 LTI 启动地址 → 每秒看一次是否落到 `webassign.net`（最多 20 次）。约 10 秒，不需要任何密码 | `external/webassign.py`、`bot/conversation.py::_webassign_launch_url`、`canvas/api.py::sessionless_launch` |
| 学生发来答案纸照片（且正处于 WebAssign 一轮中） | `tg.download()` 取图 → `transcribe_with_consensus()` → 默认只有一个便宜读者 `gemini-flash-lite` → 一次 HTTPS 调 Gemini `generateContent` | `mathinput/pipeline.py`、`mathinput/providers/gemini.py` |
| 启动后 30 分钟（之后每 30 分钟） | `keepalive.ping()`：`auth_from_live_browser()` —— 这会在 `canvas-keepalive` 线程里**再起一个**属于该线程的 Playwright 驱动并 attach 9222 读 cookie；读不到就用磁盘快照；再 `GET /api/v1/users/self`，把 Canvas 轮换过的 cookie 写回快照 | `canvas/keepalive.py`、`canvas/session.py` |
| 某次浏览器操作超出预算 | 看门狗放弃该 worker；`_restart_browser()` 以子进程方式跑 `browser_start.py --restart`（仍是 headless）；`browserthread.reset()`；下一次操作新建 `browser-2` 和新的驱动。代价：WebAssign 会话又没了，再付一次约 10 秒 | `external/webassign.py::_with_tab` |
| Canvas token 中途失效 | `_fall_back()` → `recover_auth()` → 切到 cookie 通道，并在 Telegram 里明说 | `bot/conversation.py`、`canvas/api.py` |

---

## 3. 清单 (b)：外部软件、进程与网络服务

### 进程

| 进程 | 谁启动的 | 什么时候 | 活多久 |
|---|---|---|---|
| `powershell.exe`（跑 `autostart.ps1`，其中内联执行 `bot_restart.ps1`） | Windows Shell，经 Startup 文件夹的 `.lnk` | 登录时 | 几十秒到约 3 分钟，做完就退出 |
| `python.exe`（`browser_start.py`） | autostart.ps1 | 阶段 A 第 3 步 | 浏览器端口起来就退出（最多约 20 秒） |
| `chrome.exe` ×N —— Playwright 自带的 Chromium，`--headless=new --remote-debugging-port=9222`。一个浏览器主进程加若干渲染器/网络等子进程 | browser_start.py，**脱离式** | 阶段 B | 常驻。不依赖任何父进程 |
| `python.exe`（`-m ainl.bot`） | bot_restart.ps1 的 `Start-Process -WindowStyle Hidden` | 阶段 C | 常驻。**同一时刻只能有一个** |
| Playwright 驱动（Playwright 包里自带的 `node.exe`，bot 进程的子进程） | `sync_playwright().start()`，在 `util/pw.py` | **LAZY**：第一次浏览器操作时（`browser-N` 线程）；另一个在首次 keepalive ping 时（`canvas-keepalive` 线程）；cookie 通道下 MainThread 在启动时就有一个 | 跟随 bot 进程。被看门狗放弃的 worker 的驱动留到进程结束 |
| `python.exe`（`browser_start.py --restart`） | bot 内的看门狗 | **LAZY**：仅当浏览器卡死 | 短命 |
| `powershell.exe`（短命，查询 chrome.exe 命令行） | `browser_start.py --restart` 在没有 `psutil` 时；或 `session.py::_refuse_if_profile_in_use` | **LAZY** | 短命 |

### 网络服务

| 服务 | 发起者 | 协议 | EAGER / LAZY |
|---|---|---|---|
| DNS 解析 `canvas.wisc.edu` | autostart.ps1 | 系统 DNS | EAGER |
| `127.0.0.1:9222/json/version`（本机回环） | browser_start.py、autostart.ps1 | HTTP | EAGER |
| `www.webassign.net`（`START_URL`） | chrome.exe 自己 | HTTPS | EAGER（只是加载页面；重启后处于登出状态） |
| Canvas REST `canvas.wisc.edu/api/v1/...`（以及 GraphQL） | bot（httpx） | HTTPS，Bearer token 或 cookie | EAGER：仅 `users/self/profile` 一次。其余（planner、assignments、提交、sessionless launch）全 LAZY |
| Telegram Bot API `api.telegram.org` | bot（httpx，单个 `Client`，超时 60 秒） | HTTPS；`setMyCommands` 一次，然后 `getUpdates` 长轮询（`timeout=30`，只订阅 `message` 与 `callback_query`） | EAGER 且持续 |
| CDP：`127.0.0.1:9222` 的 WebSocket | Playwright 驱动 | 本机回环 | LAZY（cookie 通道下 EAGER） |
| WebAssign / Cengage（`webassign.net`，以及 LTI 跳转链上的 Cengage 域名） | **chrome.exe**，由 bot 经 CDP 驱动 | HTTPS | LAZY |
| Canvas LTI sessionless launch | 先是 bot 向 Canvas REST 要启动地址，再由 chrome.exe 去打开 | HTTPS | LAZY；重启后的第一次 WebAssign 操作必走 |
| Gemini API `generativelanguage.googleapis.com`（`generateContent`，密钥 `GEMINI_API_KEY` 走请求头） | bot（httpx） | HTTPS | LAZY：**只在读答案纸照片时**。整个系统唯一的 LLM 调用 |

代码里默认的升级读者是 `claude-sonnet-5`（`mathinput/pipeline.py`）。`bot_restart.ps1` 的装载列表里没有它对应的密钥名；若进程环境里没有配置，升级一步会得到 `ProviderNotConfigured`，结果标为需要人工确认，不会发出请求。

---

## 4. bot 进程里的线程

| 线程名 | 何时创建 | 类型 | 干什么 | 拥有的 Playwright 驱动 |
|---|---|---|---|---|
| `MainThread` | 进程启动 | 主线程 | `getUpdates` 长轮询；**所有**消息处理都在这一个线程上串行（所以一次慢的浏览器调用会让整个聊天看起来死了 —— 这正是看门狗存在的原因） | token 通道：无。cookie 通道：启动时就有一个 |
| `canvas-keepalive` | `keepalive.start()`，启动时 | daemon | 先睡 N 分钟（默认 30），再 `ping()`，如此往复。任何异常只记 warning，绝不拖垮 bot。结果记一行 `keepalive: session alive/GONE` | 首次 ping 时创建自己的一个（`pw.py` 用 `threading.local`，线程之间不借用） |
| `browser-N`（`browser-1`、`browser-2`…） | **按需**：第一次 `browserthread.run()` | daemon | 从队列里一次取一个浏览器任务执行。调用方只等预算时间；超时则把该 worker 标记 `dead` 并**放弃**（Python 杀不了线程），下一次调用新建 `browser-(N+1)` 和全新的驱动 | 每个 worker 一个 |
| `ThreadPoolExecutor` 的临时线程 | 每次读照片时 | 临时 | 并行跑各个「便宜读者」；默认只有 Gemini 一个 | 无 |

「每线程一个驱动」的原因（`util/pw.py` 头注释）：同一进程里 `sync_playwright()` 退出后再进第二次会在驱动拆除期间 EPIPE；而 sync Playwright 对象又只属于创建它的线程。所以驱动启动一次就留着，CDP 连接缓存复用、不 close。

---

## 5. `~/.ainl/` 下的文件

（目录可用 `AINL_HOME` 改位置。以下内容**根据代码描述**，没有打开任何实际文件。）

| 路径 | 谁写 | 谁读 | 内容 / 行为 |
|---|---|---|---|
| `autostart.log` | `autostart.ps1` | 人 | **追加**。带时间戳的 `autostart begin` / `network: ...` / `browser: ...` / `bot: ...` / `autostart end` |
| `bot.log` | `bot_restart.ps1` 的 stdout 重定向 | 人 | 每次启动**覆盖**。代码用 `logging`，默认走 stderr，所以这个文件通常几乎是空的 |
| `bot.log.err` | 同上，stderr 重定向 | 人；`bot_restart.ps1` 读最后 2 行 | 每次启动**覆盖**。**真正的运行日志在这里**（`bot up for chat ...`、`keepalive every 30 min`、异常堆栈）。注意那行「bot up」会带上 chat id，所以这个文件不要外传 |
| `bot_state.json` | bot：每处理一条 update 后保存，退出时再保存一次 | bot 启动时 `BotState.load()` | `offset`（Telegram 游标，保证重启后不重复处理旧消息）+ 每个聊天的 menu / pending / draft / math。图片不在里面，只在内存 |
| `channel.json` | `/channel <auto\|token\|cookie>`；或启动时钉死的通道打不开被自动改回 `auto` | bot 启动时、每次 `/channel` | 形如 `{"prefer": "..."}`。不存在 = `auto` |
| `lti-map.json` | `external/link.py::dep_for`（LTI 解析成功后） | 同一函数，先查它 | `"<course_id>:<assignment_id>"` → WebAssign 的 `dep`。id 不会变，所以永久缓存；命中就省掉一次约 6 秒的启动 |
| `chrome-profile/` | chrome.exe | chrome.exe | `--user-data-dir`。持久 cookie（Canvas 登录）在这里，跨重启保留；**会话级** cookie（WebAssign 的 `JSESSIONID`、`cmp-session-id`）不落盘，浏览器进程一结束就没 |
| `<host>.storage_state.json` | 交互登录时 `login_interactive()`；`keepalive.ping()` 与 `ApiSource.close()` 把轮换后的 cookie 合并回去 | cookie 通道在读不到活浏览器时的退路 | Canvas 会话快照。**等同密码**，当凭据对待 |
| `wedge-captures.jsonl` | `browserthread.capture_wedge()`，仅当 `AINL_WEDGE_CAPTURE` 开启（或开了详细计时） | 人 | 每次卡死追加一行 JSON 证据；URL 被截到 `scheme://host/path`，因为这些页面的 query string 是凭据 |
| `login-debug/` | `python -m ainl.canvas --login --debug` | 人 | 登录各跳的截图；正常启动不涉及 |

---

## 6. 怎么确认它成功了 / 怎么关掉

**确认**

1. 看 `~/.ainl/autostart.log` 的最后一段：应依次有 `autostart begin` → `network: True` → `browser: browser up on 9222 ...`（或 `already running on 9222`）→ `bot: bot processes: 1` → `autostart end`。
2. `bot processes: 0`，或者后面跟着的两行是报错 → 去看 `~/.ainl/bot.log.err`。
3. 最直接的验证：在 Telegram 里发 `/todo`。有回复就说明 Canvas 和 Telegram 两头都通。
4. 想验证浏览器那一半：回复一个 🧮 编号。重启后的第一次会多等约 10 秒（见下）。

**关掉自启动**

```powershell
scripts\autostart_install.ps1 -Remove
```

或者 Win+R → `shell:startup`，手动删掉 `ainl-autostart.lnk`。两种做法都只是让**下次登录**不再自启；当前已经在跑的 bot 和浏览器不受影响。

**重新装上**：`scripts\autostart_install.ps1`（不带参数）。

---

## 7. 这套东西**不**负责的事

- **登录之前不会启动。** 机器开机但停在锁屏/登录界面时，什么都没在跑。这是有意的：密钥在 User 环境变量里，profile 属于这个用户。
- **Canvas 浏览器会话过期时，必须有人在场。** NetID + Duo 是人做的，代码替不了。办法是 `python scripts/browser_start.py --show` 开一个可见窗口登录（浏览器已在跑时，先 `--restart --show`，因为同一个 profile 不能被两个浏览器同时占用）。只用 token 通道时平时感觉不到这一点：启动地址是 bot 用当前通道（通常是 token）向 Canvas REST 要来的一次性 URL，自带 verifier（**它是凭据，代码从不记录它，本文也不写出来**），再交给浏览器打开。只有 token 失效、需要降级到 cookie 通道时，才会撞上这条限制。
- **断电/重启后，第一次 WebAssign 操作更慢。** 会话级 cookie 随浏览器进程消失，第一次使用时经 Canvas LTI sessionless launch 重建，约 10 秒，不需要密码。之后恢复正常。看门狗每次重启浏览器也会付同样的代价。
- **没有守护进程。** bot 启动后如果崩溃或自己退出，没有任何东西会把它再拉起来，直到下次登录或手动运行 `scripts\bot_restart.ps1`。典型场景：2 分钟内网络还没通，`whoami()` 抛出的网络异常不属于 `ConfigError`/`CanvasAuthError`，进程直接带着堆栈退出（堆栈在 `bot.log.err`）。运行起来之后则相反：`getUpdates` 的网络抖动只会睡 5 秒重试，单条消息处理中的异常也被 `handle()` 兜住，不会结束进程。
- **autostart 不检查结果。** `network: False` 或浏览器没起来时，脚本仍会继续去启动 bot，只是把情况写进日志。
- **不更新代码、不装依赖。** 它用 PATH 上的 `python` 跑仓库里当前的代码；Playwright 的 Chromium 必须事先 `playwright install chromium` 装好。
- **不处理重复登录之外的并发。** 手动再起第二个 `python -m ainl.bot` 会让两个实例都收到 409；永远通过 `bot_restart.ps1` 重启。
