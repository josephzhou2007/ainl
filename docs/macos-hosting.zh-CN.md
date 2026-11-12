[English](macos-hosting.md) · **简体中文**

# macOS 托管（Mac mini）

最后核对：2026-09-24。Windows 上的对应说明见 `docs/autoopenexplain.md`（`.ps1` 脚本仍保留，两边并存）。

## 一、一次性安装

```bash
cd ~/code/AINATIVELEARNINGOS
/opt/homebrew/bin/python3.12 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m playwright install chromium
.venv/bin/python -m pytest tests canvas-official/tests scale-up/tests "Cookie Management/tests" -q   # 284 passed
```

> Cookie Management 目录另有自己的依赖：`.venv/bin/python -m pip install -r "Cookie Management/requirements.txt"`。
> 用项目自己的 `.venv`，不要 `pip install -e` 到系统 Python；`.venv/` 已在 `.gitignore`。

## 二、密钥放 `~/.ainl/env`（对应 Windows 的用户环境变量 `setx`）

```bash
scripts/macos/service.sh install        # 第一次会把 scripts/macos/env.example 复制成 ~/.ainl/env（chmod 600）
$EDITOR ~/.ainl/env                     # 填 CANVAS_TOKEN / TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID / GEMINI_API_KEY
scripts/macos/service.sh restart bot
```

`run.sh` 在启动每个服务前 `source` 这个文件。**密钥永远不进 plist、不进代码、不进日志、不进聊天。**

## 三、三个 launchd 服务（对应 Windows 的三个 .ps1）

| macOS | 做什么 | Windows 对应 |
|---|---|---|
| `com.ainl.browser` | 无头 Chromium，端口 9222，`--foreground` 由 launchd 监督 | `autostart.ps1` 里的 `browser_start.py` |
| `com.ainl.bot` | 自己的私人作业 bot `python -m ainl.bot` | `bot_restart.ps1` |
| `com.ainl.public-bot` | 同学注册用的公开 bot `scale-up/bot.py`（需要 `TELEGRAM_PUBLIC_BOT_TOKEN`） | `public_bot_restart.ps1` |

```bash
scripts/macos/service.sh install                 # browser + bot（默认）
scripts/macos/service.sh install public-bot      # 需要时再加
scripts/macos/service.sh status
scripts/macos/service.sh restart bot             # 只重启 bot，不碰浏览器
scripts/macos/service.sh restart browser         # 换一个干净的渲染器（WebAssign 会话会丢，LTI 自动重建 ~10s）
scripts/macos/service.sh logs bot                # tail ~/.ainl/bot.log.err —— 真正的运行日志在这里
scripts/macos/service.sh stop bot / start bot
scripts/macos/service.sh remove                  # 全部卸载
```

和 Windows 方案的差别：

- **launchd 保证同一个 label 只有一个实例**，所以 `restart bot` 天然不会出现两个 `getUpdates` 消费者（Windows 上要按命令行找旧进程杀）。
- **崩了会自动拉起**（`KeepAlive.SuccessfulExit=false`，间隔 15 秒）。Windows 版没有守护，bot 崩了要等下次登录。
  代价：配置错误（比如 token 没填）也会每 15 秒重试一次——看 `logs bot` 就知道原因。
- 开机时 `run.sh bot` 先等网络（最多 2 分钟）、再等浏览器（最多 20 秒），和 `autostart.ps1` 顺序一致。
- `browser_start.py --restart`（看门狗在浏览器卡死时调用）会检测到浏览器由 launchd 托管，改用 `launchctl kickstart -k`，
  不会在 launchd 的浏览器旁边再起一个抢 9222。
- 日志是**追加**的（Windows 版每次启动覆盖），久了自己清。

在登录会话（`gui/<uid>`）下运行，不是系统级 daemon：浏览器 profile 和密钥都属于这个用户；机器要保持登录状态。
本机已设 `pmset sleep 0`，不会休眠。

## 四、需要人手动登录时

cookie 通道首次需要 NetID + Duo。Mac mini 一般没人坐在前面，两种办法：

```bash
scripts/macos/service.sh stop browser
.venv/bin/python scripts/browser_start.py --show      # 开一个可见窗口（走屏幕共享登录）
# 登录完成后关掉它：
.venv/bin/python -c "import sys;sys.path.insert(0,'scripts');import browser_start as b;from ainl.config import home;b.kill_profile_holders(home()/'chrome-profile')"
scripts/macos/service.sh start browser
```

token 通道（当前主通道）不需要这一步。

## 五、公网入口（webhook / OAuth 回调，M9 之后才需要）

`ONBOARDING.md` 已定：Cloudflare Tunnel。本机已装 `cloudflared`，且已有别的 tunnel 在跑（`~/Library/LaunchAgents/com.example.cloudflared.plist`），
到时候给现有 tunnel 加一条 ingress 即可，不用再开一个。

## 六、跨平台改动记录（2026-09-24）

| 文件 | 改了什么 |
|---|---|
| `scripts/browser_start.py` | 按 OS 找 Playwright 的 Chromium；POSIX 下用 `start_new_session` 脱离；按 profile **完整路径**杀进程（`ps -ax -o pid=,command=`，不按进程名）；UA 按 OS；新增 `--foreground`；`--restart` 识别 launchd |
| `scale-up/browsers.py` | 复用上面的 `command()` / `spawn()` / `kill_profile_holders()`，去掉自己的 Windows 分支 |
| `src/ainl/canvas/session.py` | 非 Windows 的「profile 是否被占用」检查从 `pgrep -af` 改成 `ps -ax -o command=`。macOS 的 `pgrep -a` 是「包含祖先进程」，只输出 PID，原来的检查在 Mac 上**永远不会命中** |
| `pyproject.toml` | 补声明 `pillow`（`gradescope.py` / `webassign.py` 一直在用，之前没写） |
| `.gitignore` | `.venv/` |

## 七、实时监控（`/monitor`，2026-09-26）

同学越多，每人一个无头 Chromium（内存主要在 renderer/GPU/utility 子进程里），24 GB 会悄悄用光。
公开 bot 里只有 owner 能用：

- `/monitor` —— 发一个「📊 Monitor」Mini App 按钮，打开 `https://example.com/native-bot-learning/monitor`，每 3 秒刷新：
  内存条（80% 橙、90% 红）、swap、CPU、load、「到 80% 前还能再开 ~N 个浏览器」（按当前同学浏览器的平均占用估）；
  每个人：学校、批准/已连 Canvas、浏览器 MB 和端口、是否正在登录、助手在不在、排队几条、最近活动、今天的消息/提交/Canvas 请求/错误；
  进程分组：公开 bot、私人 bot、owner 的 9222 浏览器、每个同学的浏览器（按 `--user-data-dir=.../users/<id>/chrome-profile`
  或端口 93xx 认，**整棵进程树求和**）、Playwright driver、cloudflared、孤儿浏览器（崩溃后没人管的 93xx），其余归「Everything else」。
  MB 优先用 macOS 的 footprint（`top`，和活动监视器一致），拿不到才用 RSS。
- `/monitor text` —— 同样内容发成一条消息（网页打不开时用；登录页没配置时 `/monitor` 也直接回文字）。
- 内存告警：每分钟查一次，越过 80% / 90% 各发一次（附最耗内存的几项），回落到 75% 以下发一次「恢复」；同一级别 30 分钟内不重复。

鉴权：页面由 LoginServer（8790，隧道唯一暴露的端口）提供；`<base>/monitor.json` 每次请求都要在 header
`X-Telegram-Init-Data` 里带 Telegram 签名的 initData，用公开 bot 的 token 验签，且用户 id 必须等于 `TELEGRAM_CHAT_ID`，
否则一律 403、不收集任何数据。initData 放在 URL 里无效；签名 2 小时后过期，页面会提示重新发 `/monitor`。
返回里没有命令行（cloudflared 的命令行可能带 tunnel token）、cookie、token、消息内容；人只有 Telegram 显示名和 chat id（和 `/users` 一样）。

数据来源：`scale-up/monitor.py`（psutil + BrowserPool/Dispatcher/LoginServer 的只读 `stats()` + 增量读 `~/.ainl/metrics`）。
公开 bot 的 metrics 记录带 `bot=public`，同学工作线程上的记录带 `uid`，所以能按人计数；提交 = Canvas 上成功的
`POST …/assignments/:id/submissions` 和讨论回复（WebAssign 在浏览器里提交，不计入）。

本机直接看一眼（不经过 bot：只有注册表里的人，没有实时状态）：`.venv/bin/python scale-up/monitor.py`（`--json` 看全部字段）。
