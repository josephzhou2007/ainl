# Cookie Management：实现说明

> 基于 Claude 最新代码重新核对。本轮只改写本 README，并在本目录新增独立代码及测试；没有修改 `src/`、`scale-up/` 或现有浏览器原型。新增模块尚未挂到运行中的 bot，也未部署远程登录网页。

## 1. 最新基础与本轮范围

Claude 已完成的能力直接复用：

| 已有模块 | 当前能力 |
|---|---|
| `scale-up/browsers.py` | 每用户 profile、CDP 端口、按需启动；`restart(chat_id)` 只重启该用户 |
| `src/ainl/util/browserthread.py` | 按 key 分配 worker；`using(key)` / `run(key=...)`，不同用户可并发 |
| `src/ainl/util/pw.py` | 连接缓存按线程及 CDP 地址区分 |
| `src/ainl/external/webassign.py` | 可注入 `cdp_url`、`launch_url`、`restart` |
| `scale-up/onboarding.py`、`tokens.py` | 私聊注册、审批、应用 token 的哈希存储及验证 |
| `scale-up/bot.py` | 单 Telegram receiver；主账号业务与其他用户 onboarding 分流 |

`browsers.py` 顶部仍有“per-user worker 未完成”的旧注释，以最新 `browserthread.py` 实现为准。新用户目前仍收到 Canvas 尚未连接的提示；浏览器隔离完成不等于登录交接与业务派发已接好。

本轮实现的是下一层 **Canvas 会话管理库**，不是重新实现浏览器池。没有启动真实浏览器、读取现有个人 Cookie、登录学校或提交作业。

## 2. 新增文件

| 文件 | 实现 |
|---|---|
| `cookie_management/integration.py` | `build_services()`，注入已有 registry/pool/token verifier，构建服务 |
| `cookie_management/sessions.py` | 按用户执行探针、首次身份确认、保活检查、Cookie 恢复；对接现有 keyed worker |
| `cookie_management/store.py` | Fernet 加密、身份绑定、版本化快照、加密临时文件与原子替换 |
| `cookie_management/tickets.py` | 应用 token 验证后签发 5 分钟、单次使用、绑定用户及 token 版本的登录票据 |
| `tests/test_sessions.py` | 假浏览器测试认证、隔离、轮换、恢复、异常与票据；不访问学校 |
| `requirements.txt` | 新增加密依赖，未改根目录依赖 |

`SessionManager` 是进程内单实例服务：同一用户串行，不同用户独立。这个锁目前只覆盖本库调用；接入 viewer/WebAssign 时，还需在调度层建立共同的人工控制与任务锁。

## 3. 在现有程序中组合服务

以下是未来接线示例，不是当前已上线的入口。新目录目前不在根项目的 setuptools 包发现范围内；接入入口需显式加入路径，后续再决定正式打包位置。

```python
# 放在未来 scale-up 入口初始化处，不能在每条请求中重新创建。
import os
import sys
from pathlib import Path

repo = Path(__file__).resolve().parents[1]  # 此示例假定入口在 scale-up/
sys.path.insert(0, str(repo / 'Cookie Management'))

from cookie_management.integration import build_services
import tokens

# reg / pool 必须复用该进程管理业务浏览器的 Registry / BrowserPool。
services = build_services(
    reg,
    pool,
    lambda token: tokens.verify(reg, token),
    origin='https://canvas.wisc.edu',
    encryption_key=os.environ['AINL_SESSION_KEY'].encode('ascii'),
)
```

`AINL_SESSION_KEY` 必须是合法 Fernet key，由服务端安全配置提供；不要存入用户目录、Git 或聊天，也不要每次重启生成新 key。Fernet 是认证加密，密钥丢失后不能恢复旧快照。[官方说明](https://cryptography.io/en/latest/fernet/)

已有 `Registry` 持有内存状态，所以第一版应在一个宿主进程中共享同一 registry/pool/services。不能让独立 Web 进程各自加载旧 registry、各自分配端口；未来拆进程需经过单一 supervisor/RPC 或共享状态协调。

## 4. 登录交接调用顺序

```text
已审批同学的应用 token
  → LoginTickets.issue(app_token)
  → 经身份验证的 POST redeem(ticket, app_token)
  → 返回服务器确认的 user_id
  → viewer 接入 pool.cdp_url(user_id)，取得人工控制权
  → 同学自行完成 NetID + Duo
  → sessions.inspect(user_id)
  → 向同学展示返回的 canvas_id，确认连接此账号
  → sessions.complete_login(user_id, confirmed_canvas_id)
  → ACTIVE，保存加密快照，结束 viewer 控制权
```

- `tokens.py` 发的是本应用凭证，不是 Canvas token，不替代 NetID/Duo。
- ticket 原文不保存，仅保留哈希；每人最多一个待兑换票据。应用 token 轮换后，旧票据不可用；票据只能兑换一次，服务重启自然失效。
- `redeem()` 不是 HTTP 路由，未来网关必须用 POST 并保护 Origin/CSRF；GET 链接预览不能消费票据。
- 票据兑换只确定用户，不建立持久 viewer 会话。网关还要发短期 viewer session，并对 WebSocket 连接及后续操作持续授权。
- `SessionManager` 的 `user_id` 方法是内部接口；不得把外部 URL 的用户编号原样传入。应从应用 token 验证结果或已验证 viewer session 派生。
- 未绑定时 `inspect()` 返回 `UNBOUND` 和可确认的 Canvas ID，不自动建立绑定；存在绑定后不允许静默切换账号。

## 5. Cookie 如何保存与恢复

### 请求与轮换

`PooledBrowser.run()` 使用既有 `browserthread.run(key=user_id)`，再连接 `pool.cdp_url(user_id)`。要求该独立浏览器恰好有一个 context，避免自动选择不明确的上下文。

探针通过该 context 的 `request.get('/api/v1/users/self')` 请求，`max_redirects=0`，10 秒请求超时，45 秒 worker 外层预算。Playwright 的 context request 与浏览器共享 Cookie，响应轮换会回到同一 cookie jar。[官方 API 文档](https://playwright.dev/python/docs/api/class-apirequestcontext)

返回 200 还必须解析出数字形式的账号 ID；支持仓库使用的 `while(1);` JSON 前缀。不会把登录 HTML 当成功，也不在异常结果里返回响应正文、登录输入或 Cookie。

### 快照

保存位置沿用 registry：

```text
<AINL_HOME>/users/<chat_id>/
  chrome-profile/       # 现有 BrowserPool 管理
  app-token.json        # 现有 tokens.py 管理
  canvas-session.enc    # 本轮新增会话快照
```

加密内容包括 `schema=1`、应用 user_id、Canvas origin、Canvas ID、递增 version 和 Cookie 列表。读取时校验封装身份，所以将 A 的密文复制到 B 目录会被拒绝。

**当前只保存 domain 与 Canvas host 精确匹配的 Cookie**，保留其 path/expiry/secure 等属性，排除其他域与已过期项。不保存 SSO/Duo Cookie、localStorage、IndexedDB，也不声称这是完整 storage state。相关浏览器状态仍由既有 profile 保留，重启后的实际 Duo 行为需真实验证。

写盘使用同目录加密临时文件 → flush/fsync → `os.replace`，不产生明文临时文件。损坏、密钥不匹配、身份不符会抛出 `SnapshotError`，不能按“文件不存在”静默建立新身份。

profile 本身仍含凭证；加密快照不能替代服务账号 ACL 和磁盘保护。多进程写锁、密钥轮换、备份策略未实现，首版由单一服务实例独占写入。

### 恢复

`sessions.restore(user_id)` 先探测当前浏览器：

- 当前已登录正确账号：直接保存当前新状态，不用旧快照覆盖。
- 当前已登录其他账号：返回 `IDENTITY_MISMATCH`。
- 确认 401 或重定向：导入当前快照中的 Canvas Cookie，再探测账号；恢复成功才更新快照。
- 网络异常、403、429、5xx 或无效 JSON：不导入旧 Cookie。
- 没有快照：返回 `UNBOUND`，等待首次登录。

快照不能让已被学校撤销的会话复活；恢复后仍需验证。当前将探针 3xx 统一视为需重新认证，不追踪重定向目标；若学校增加非认证重定向，应进一步分类。

## 6. 保活与故障状态

`heartbeat(user_id)` 只执行一次探针并保存轮换后的 Cookie，尚无自动线程或定时任务。未来由统一调度器调用，可先用约 30 分钟间隔，按实际情况调整；已有业务活动时减少额外请求。

| 状态 | 含义与上层处理 |
|---|---|
| `ACTIVE` | 探针成功且身份匹配；快照操作成功后才返回 |
| `UNBOUND` | 尚无确认绑定；提供登录入口 |
| `REAUTH_REQUIRED` | 401/3xx；暂停任务，通知本人重新认证 |
| `IDENTITY_MISMATCH` | 已有账号绑定与当前浏览器不符；拒绝更新 |
| `BLOCKED` | 403；检查权限，不能盲目触发 Duo |
| `DEGRADED` | 网络/其他 HTTP/格式错误；退避处理 |
| `QUARANTINED` | worker 超时；本实例拒绝该用户后续操作 |

超时隔离是因为既有 browserthread 超时只放弃等待，旧线程可能仍在运行。本库超时后不会接受迟到快照或继续派发同用户任务。恢复需由宿主确认旧任务已停止、完成受控重启后再重建服务；没有提供盲目清除隔离标记的接口。

审批撤销检查发生在浏览器访问前及保存前，但不承诺取消已执行中的网络请求。`/forget` 正式接线时必须先停止该用户的队列、viewer 和浏览器，再删除目录；仅删除快照不代表学校端全局注销。

## 7. 后续需要改动的现有位置

**下表是接线计划，本轮没有修改这些文件。**

| 位置 | 需要接入的内容 |
|---|---|
| `scale-up/bot.py` | 初始化并复用 registry/pool/services；单 receiver 按用户派发；新用户独立 Conversation/BotState；加入探针调度和去重重登通知 |
| `scale-up/onboarding.py` | 将已批准后的 coming-soon 提示改为连接入口；`/forget` 先撤销活动会话和任务再删除目录 |
| `scale-up/browsers.py` | restart/reap/shutdown 与 checkpoint/restore 协调；回收前保存，启动后恢复；验证端口归属和遗留进程。不要在 pool 内锁中回调会再次调用 pool 的 SessionManager，避免死锁 |
| `src/ainl/bot/conversation.py` | 注入用户专属 Canvas client、WebAssign factory、通道策略；不再让新用户走默认 9222 或全局通道 |
| `src/ainl/canvas/api.py`、`_http.py` | 可注入共享 BrowserContext 的业务 transport；保持分页、上传、CSRF、错误和回执语义；当前本库仅探针共享 Cookie，尚未迁移业务请求 |
| `src/ainl/canvas/channel.py`、`keepalive.py` | 为多人入口显式传入用户上下文，或由新入口完全替代其全局行为；保留主账号旧入口兼容 |
| 根 `pyproject.toml` | 正式接入时纳入新包和依赖、明确测试收集；当前无需改动 |

`registry.py`、`tokens.py`、按 key 的 browserthread、CDP 缓存以及 WebAssign 的注入接口可直接复用。未来还要核查 `ApiSource.recover_auth()` 的全局 fallback，防止错误切回主账号。

## 8. 接下来新增的远程 viewer

新的 viewer 应连接已有池的浏览器，不再次 `launch_persistent_context()` 打开相同 profile。

沿用原型 CDP screencast + 输入透传技术，但另写受控网关：

1. HTTPS/WSS 网关鉴权；CDP 保持内部可访问。
2. 每个 viewer session 只映射一个已验证用户和浏览器；仅向该用户发送画面。
3. 人工控制租约期间暂停后台操作；支持实际 SSO/Duo 流程产生的新页面。
4. 不复制原型 `/status` 中的 `activeElement.value` 读取；禁用登录输入、页面正文、票据和 Cookie 日志。
5. 原型 `/agent/read`、`/snapshot`、`/shutdown` 不直接开放；变更接口需授权及 CSRF/Origin 校验。
6. 限制导航/重定向与网络出口，禁止访问宿主内部服务；实际学校登录域先测定再配置。
7. 登录完成调用本库确认绑定，撤销 viewer 控制权，再恢复业务队列。

这一层、完整业务 transport、提交幂等核对及生产撤销编排仍待实现。因此目前可运行的是测试和内部会话接口，还不是同学可直接打开的登录产品。

## 9. 验证方式

在仓库根目录执行：

```powershell
python -m pip install -r 'Cookie Management/requirements.txt'
python -m pytest 'Cookie Management/tests' scale-up/tests tests/test_browserthread_keys.py tests/test_session.py tests/test_auth.py -q
```

本次环境默认 pytest 临时路径遇到 Windows 权限问题，改用仓库内独立临时目录完成验证：

```powershell
python -m pytest 'Cookie Management/tests' scale-up/tests tests/test_browserthread_keys.py tests/test_session.py tests/test_auth.py -q -p no:cacheprovider --basetemp='D:/code/local/projects/AI-native-learning/Cookie Management/.test-tmp-01'
```

`--basetemp` 必须指向仅供测试使用的目录，pytest 会清理该目录；不得指向真实用户数据。测试覆盖首次绑定、两用户并发隔离、Cookie 轮换、密文篡改/跨用户复制、写盘失败、认证错误分类、恢复、超时隔离、撤销审批，以及票据跨用户/重放/到期/token 轮换。

本轮相关测试结果：**67 passed**，包含新增会话测试及既有浏览器池、注册、工作线程与认证测试；`git diff --check` 通过。

真实 Canvas/Duo 登录、浏览器重启恢复、持续运行资源占用及作业提交尚未实测。
