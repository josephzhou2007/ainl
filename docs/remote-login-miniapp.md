# 远程登录（cookie tunnel）× Telegram Mini App —— 设计

状态：**登录本身已实现并在本机跑通（2026-09-25），还差公网入口**。见文末「实现状态」。
前置阅读：`docs/optimization-goals.md`（未做 · 多用户）、`Cookie Management/README.md`、`scale-up/`、`ONBOARDING.md`。

## 要解决什么

同学用公开 bot（`scale-up/bot.py`）时，bot 要替他在 Canvas / WebAssign 上干活，就得有**他自己的 Canvas 登录状态**。
方向早已定下（`optimization-goals.md`）：先走 cookie 通道，不等学校签发 token / OAuth。难点只有一个：

> 登录必须由同学本人完成（NetID + 密码 + Duo），不能让他把密码交给我们，也不能在服务器上替他输入。

做法：**同学在 Mac mini 上属于他自己的那个浏览器里亲手登录**，我们把那个浏览器的画面实时传到他手机上。
登录完成后 cookie 留在他自己的浏览器 profile 里，之后 bot 用它工作。

## 用户看到的流程

1. 在公开 bot 里注册 → 主人批准（已有：`scale-up/onboarding.py`）。
2. bot 发一个按钮「🔐 登录 Canvas」。
3. 点按钮 → **Telegram 里弹出我们的 Mini App 页面**（不离开 Telegram），里面是 Mac mini 上**他的**浏览器，停在 UW NetID 登录页。
4. 他在页面里点、打字 —— 操作实时传给那个浏览器；Duo 推到他手机，批准后回到 Telegram。
5. 登录成功 → bot 用这个会话问 Canvas「我是谁」，**向他确认 Canvas 账号就是他本人** → 保存 → 页面关闭。
6. 之后 `/todo`、WebAssign 等都用他自己的会话；`/forget` 一键删除一切（已有）。

## Telegram 支持吗 —— 支持（官方文档核对于 2026-09-25）

| 需要的能力 | 结论 | 官方文档 |
|---|---|---|
| 在 bot 里弹出网页 | Mini App（Web App），在 Telegram 内打开 | https://core.telegram.org/bots/webapps |
| 从按钮打开 | 内联按钮 `web_app`：**只能在用户和 bot 的私聊里用**——正好是我们的场景 | https://core.telegram.org/bots/webapps#inline-button-mini-apps · https://core.telegram.org/bots/api#inlinekeyboardbutton |
| 从菜单打开（常驻入口） | 菜单按钮 `MenuButtonWebApp`（`setChatMenuButton`） | https://core.telegram.org/bots/webapps#launching-mini-apps-from-the-menu-button · https://core.telegram.org/bots/api#menubuttonwebapp |
| 地址要求 | `WebAppInfo.url`：**必须是 HTTPS** | https://core.telegram.org/bots/api#webappinfo |
| 确认是谁在用 | 页面拿到 `Telegram.WebApp.initData`，服务器用 HMAC-SHA256 校验（密钥 = HMAC-SHA256("WebAppData", bot token)），并检查 `auth_date` 新鲜度 | https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app |
| 不用 bot token 也能校验 | `signature` 字段（Ed25519），官方公钥 | https://core.telegram.org/bots/webapps#validating-data-for-third-party-use |
| 画面够大 | `expand()`、`requestFullscreen()` | https://core.telegram.org/bots/webapps#initializing-mini-apps |

`initData` 的签名解决了「谁在登录」：服务器从签名里拿到可信的 Telegram user id，直接对应 `registry` 里的那个人，
不需要额外的登录链接或票据（`Cookie Management/cookie_management/tickets.py` 可以作为备用或删减）。

## 架构

```
同学的 Telegram ──(Mini App, HTTPS)──▶ Cloudflare Tunnel ──▶ Mac mini 上的 viewer 服务 (本机端口)
                                                               │  1. 校验 initData → user id → registry
                                                               │  2. pool.cdp_url(user id)  → 他的浏览器
                                                               │  3. CDP Page.startScreencast → 画面帧 → WebSocket → 页面 <canvas>
                                                               │  4. 页面上的点击/键盘 → WebSocket → CDP Input.dispatch*Event
                                                               ▼
                                                  他自己的 headless Chromium（~/.ainl/users/<id>/chrome-profile）
```

| 部分 | 现状 | 说明 |
|---|---|---|
| 每人一个浏览器、独立 profile、按需启动、闲置回收 | **已有** `scale-up/browsers.py` | 端口从 9300 起，绝不用 9222 |
| 每人一个 Playwright worker（互不排队） | **已有** `util/browserthread.py` 的 key | |
| cookie 快照加密、身份绑定、保活、恢复 | **已有库，未接入** `Cookie Management/` | Fernet，需 `AINL_SESSION_KEY` |
| viewer 服务（HTTP + WebSocket） | **已写** `scale-up/viewer.py` | 只绑 127.0.0.1，由 tunnel 暴露 |
| Mini App 页面（画面 + 输入转发） | **已写** `scale-up/login.html` | 点=点击、拖=滚动、隐藏 textarea 接键盘 |
| 公网 HTTPS | **要配** | 本机已有 cloudflared（`com.example.cloudflared`），加一条 ingress |
| 登录完成检测 + 本人确认 | **已接上** `sessions.inspect / complete_login` | 用 `/api/v1/users/self`，页面上显示 Canvas 名字让本人确认 |
| 分发器：公开 bot 按 user id 路由到各自的 `Conversation` | **要写** | 多人同时 `/auto` `/sweep` 的前提 |

画面与输入用的 Chrome DevTools Protocol（不需要 VNC）：
- 画面：https://chromedevtools.github.io/devtools-protocol/tot/Page/#method-startScreencast
- 鼠标：https://chromedevtools.github.io/devtools-protocol/tot/Input/#method-dispatchMouseEvent
- 键盘/文字：https://chromedevtools.github.io/devtools-protocol/tot/Input/#method-insertText
- Cloudflare Tunnel：https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/

## 必须守住的

- **密码只进 UW 的页面**：我们只转发按键，不记录、不保存键盘输入（日志、transcript 都不能有）。
- 每个 viewer 会话**只能连到这个人自己的浏览器**；user id 只从校验过的 `initData` 来，绝不信任网址里的参数。
- 会话短时有效（几分钟），登录完成或超时就断开并收回控制权。
- cookie 快照加密保存；`/forget` 删除浏览器 profile + 快照 + registry；写一页隐私说明（存什么、不存什么、怎么删）。
- 一个 bot token 只有一个 `getUpdates` 消费者：分发器在**一个**公开 bot 进程里，不要起第二个。

## 现实问题

- 登录发生在 Mac mini 上：UW 看到的是新设备 + 你家的 IP，第一次 Duo 可能多问；登录时勾「记住这台设备」。
- 手机小屏操作远程浏览器会有一点延迟；只登录一次，可以接受。
- 内存：一个热浏览器约 850 MB（`scripts/metrics_report.py`，2026-09-25）；这台 24 GB 的机器大约同时 9 个。登录用的浏览器登录完就交给池子按闲置回收。

## 建议的实现顺序

1. **分发器**：公开 bot 按 user id 路由到各自的 `Conversation`（各自的浏览器、各自的 worker）；先让已批准的同学用 **token** 跑通 `/todo`。
2. **viewer 本地版**：只在本机 `http://127.0.0.1` 上把某个用户的浏览器画面和输入跑通（桌面浏览器测试）。
3. **Mini App 外壳 + initData 校验 + Cloudflare Tunnel**：从 Telegram 打开同一个 viewer。
4. **登录完成 → 本人确认 → 加密快照**：接上 `Cookie Management`。
5. 用第二个 Telegram 账号当「同学」走一遍；再请 2–3 位同学同时用 `/auto` `/sweep`，用 `/stats` 和 `metrics_report.py` 找浏览器上限。


## 实现状态（2026-09-25）

用户看到的按钮叫 **🔐 Canvas login**（不出现 "web app" 字样）；`/login` 或批准后的任何消息都会给这个按钮。

| 文件 | 做什么 |
|---|---|
| `scale-up/initdata.py` | 校验 Telegram `initData`（HMAC，15 分钟新鲜度），返回可信的 user id |
| `scale-up/viewer.py` | `LoginServer`（HTTP `/login` + WebSocket `/ws`，跑在公开 bot 进程里的一个线程）；`BrowserView`（原始 CDP：自己开一个标签页、手机尺寸、screencast、输入、跟随弹窗）；`LoginPolicy`（导航白名单） |
| `scale-up/login.html` | 手机页面：画面、点/拖/键盘、「Canvas 说你是 X，是你吗？」 |
| `scale-up/bot.py` | 建 `BrowserPool`（新启动的浏览器自动 `restore`）、`Cookie Management` 服务、启动 viewer；`/forget` 先关 viewer 和浏览器再删目录 |
| `scale-up/browsers.py` | `on_start` 钩子（锁外调用）、`stop(uid)`、分配端口时跳过已被占用的端口（崩溃后遗留的别人的浏览器） |

导航（只管顶层页面和 iframe 文档，不管图片脚本）：**先放行、再收紧**。默认任何 https 网站都放行，每一跳的域名记进日志（`login tab visited <host>`，只记域名）；非 https（`file:`、`chrome:`、`data:`、`http:`）一律拒绝，因为它们通向这台机器而不是网站。`LoginServer(restrict_hosts=True)` 时只放行名单：`canvas.wisc.edu`、`*.wisc.edu`、`*.duosecurity.com` / `*.duo.com`、`*.instructure.com`、`*.canvaslms.com`。

2026-09-26 真实登录（NetID + Duo）实测的跳转链：`canvas.wisc.edu` → `sso.canvaslms.com` → `login.wisc.edu` → `api-*.duosecurity.com` → `login.wisc.edu` → `canvas.wisc.edu`，确认「是你吗」后加密保存成功，日志里没有任何输入内容。
（WebAssign、McGraw Hill 等是 bot 之后自己在后台打开的，不经过这个登录页，也不受这里的限制。）

守住的：user id 只来自签名；每人同时只有一个 viewer；最长 10 分钟、3 分钟没操作就关；关的时候只关它自己开的标签页；按键不进日志（`websockets` 的 DEBUG 日志被强制关掉，测试里检查过）；焦点检测只问「焦点是不是输入框」，从不读值；URL 只显示 host+path，不显示 query（SAML/Duo 参数在里面）。

本机验证：`AINL_HOME=<临时目录> .venv/bin/python scale-up/viewer.py --dev-user dev_login --base-path /native-bot-learning [--with-sessions]`，桌面和手机尺寸下 UW 登录页正常显示，点击、输入、退格、滚动、键盘收放都正确，断开后标签页被关掉，日志里没有输入内容。没有用真实账号登录。

还没做：

1. **公网入口**：cloudflared 加一条 ingress（`example.com` + path `^/native-bot-learning(/|$)` → `http://127.0.0.1:8790`；`~/.ainl/env` 已设 `AINL_LOGIN_URL=https://example.com/native-bot-learning/login` 和 `AINL_SESSION_KEY`）。
2. **用第二个 Telegram 账号完整走一遍**（真实 NetID + Duo）。
3. 回收前先 `heartbeat` 存一次快照（现在靠登录时和心跳时保存；回收后靠 `restore`）；定时 `heartbeat`。
4. ~~分发器~~ **已做（`scale-up/dispatch.py`）**：已连接的同学各有一个自己的作业助手——Canvas 用**他浏览器里**的 cookie（不碰 `CANVAS_TOKEN`），WebAssign / Gradescope 用他自己的浏览器，每人一个工作线程互不排队；`/auto` `/sweep` 强制关闭，`/stats` `/channel` `/timing` `/math` 不给用；Canvas 掉线只会让他重新 `/login`，**绝不退回到主人的通道**。每 30 分钟对正在运行的浏览器做一次心跳，掉线提醒一次。

## 多学校（2026-09-26）

批准后（以及 `/school`）bot 问「Which school's Canvas do you use?」，**让他打字输入学校名**；他发的下一条文字就是搜索（15 分钟内有效，发别的命令就取消）。之后的登录页、身份确认、加密保存、`/todo` 全部用这个学校的 Canvas；改学校会删掉旧学校的会话、要求重新登录。

**学校存在 SQLite：`<AINL_HOME>/ainl.db`**（`scale-up/schooldb.py`，每次调用单独开连接 + 一把锁，多线程安全）：

- `schools`：`key`（registry.json 里存的就是它）、`name`、`canvas_origin`（API 主机）、`start_path`、`id_name`、`login_domains`（JSON，即允许的域名后缀）、`aliases`（本地搜索用）、`status`（`verified` / `unverified`）、`source`（`seed` / `instructure` / `user_url`）、`added_by`、`created_at`、`verified_at`。种子：`uw`、`ucsb`（key 不变，旧记录照常用；在有学校选项之前连上的旧用户仍自动记为 `uw`）。
- `school_hosts`：每个学校登录标签页**实际访问过**的主机和次数（viewer 的 `on_visit` → bot 按此人的学校记录），用来学到每所学校真实的 SSO 链。默认策略不变（仍是任意 https + 记录）。
- `school_search`：谁正在被问学校（`name` / `url`）。已连接的人 `/school` 之后，`Dispatcher.wants()` 通过 `owns_text` 把下一条文字交给 onboarding，而不是作业助手。
- `school_candidates`：目录搜索结果按钮背后的行。按钮只带 `school:+<行号>`（Telegram 回调 64 字节上限，从不放 URL），并且只对当时那个聊天有效。旧的 `school:<key>` 按钮照常可用。

**搜索**：先查本地库，再查 Instructure 的公开目录（Canvas App「Find your school」用的同一个：`GET https://canvas.instructure.com/api/v1/accounts/search?search_term=…`，不需要登录），按域名去重，最多 5 个按钮 + 「Not listed? Send your Canvas address」。点目录里的学校 → 先检测（下面）→ 存为 `verified`/`instructure`。贴地址 → 检测通过才存：如果这个域名在目录里也有，就算 `verified`；否则存 `unverified`/`user_url` 并通知主人（主人 `/verify <key>`）。搜索和检测在后台线程里跑，不卡 getUpdates。

**检测（`scale-up/detect.py`，安全关键）**：绝不让用户让服务器去打开任意网站。每一跳（包括每个重定向目标）都要：`https`、正常主机名（不接受 IP、端口、用户名、`\`、`%`）、解析出的**每个**地址都是公网地址（拒绝 loopback / 私网 / link-local / 100.64/10 / 组播 / 保留）；连上之后再核对实际对端地址。请求不带 cookie，超时短，最多 5 跳，响应最多读 512 KB。身份提供方（IdP）只看 Location，不去请求。

判定是 Canvas：`GET /api/v1/users/self` 未登录时 **401 + Canvas 的 JSON 错误体**（`{"status":"unauthenticated","errors":[…]}`）**+ 至少一个 Canvas 响应头**（`x-canvas-meta` / `x-request-context-id` / `x-canvas-user-id`），三者都要。起始路径：看 `/login`——如果 Canvas 自己的 `/login/<provider>` 直接跳到 IdP，就从 `/` 开始；如果 `/login` 跳到介绍页（或自己就是一个页面）且上面有 `/login/<provider>` 链接，就从 SSO 那个开始（目录给的 `authentication_provider` 优先，其次 `saml`，不选 `canvas`/`ldap` 密码表单）。

2026-09-26 实测：

| 地址 | 结果 | Canvas | 从这里开始 | 看到的跳转 |
|---|---|---|---|---|
| canvas.wisc.edu | 401 + JSON + 头 | `https://canvas.wisc.edu` | `/` | /login → /login/saml → login.wisc.edu |
| ucsb.instructure.com | 同上 | `https://ucsb.instructure.com` | `/login/saml` | /login → www.canvas.ucsb.edu（链接 saml、canvas）；/login/saml → passport.identity.ucsb.edu |
| ucla.instructure.com | 302 → bruinlearn | `https://bruinlearn.ucla.edu` | `/` | /login → /login/saml → shb.ais.ucla.edu |
| canvas.stanford.edu | 同上 | `https://canvas.stanford.edu` | `/login/1`（「SUNet Login」） | /login → canvas-gateway.stanford.edu（链接 1、11） |
| www.wisc.edu | 404 HTML | 拒绝 | | |

主人命令：`/users`（带学校名），`/schools`（每所学校的状态、来源、人数、学到的登录主机），`/verify <key>`。

