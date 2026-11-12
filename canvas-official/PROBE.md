# Token 能力实测（2026-09-19）

`python -m canvasofficial --probe`，UW-Madison Canvas，学校批准的 personal access token。

| 端点 | 结果 | n | 备注 |
|---|---|---|---|
| `users/self/profile` | PASS | 18 | |
| `users/self/favorites/courses` | PASS | 13 | 含 10 门 orientation/合规类课程，上层要过滤 |
| `planner/items` | PASS | 65 | **统一信息流**，最有价值的一个读 |
| `users/self/todo` | PASS | 44 | |
| `users/self/missing_submissions` | PASS | 11 | |
| `announcements` | PASS | 3 | |
| `courses/:id/assignments` | PASS | 24 | `include[]=submission` 可用 |
| `courses/:id/tabs` | PASS | 5 | |
| `courses/:id/files` | **403** | | `user not authorized` — 见下 |
| `courses/:id/modules` | PASS | 6 | `include[]=items` 可用 |
| `submissions/self` | PASS | 31 | |
| `POST /api/graphql` | **PASS** | | **Bearer token 可用** |
| `users/:id/page_views` | **403** | | `user not authorized` — 见下 |

**11/13 可用。**

## 三条结论

### 1. GraphQL 接受 Bearer token —— §Gaps 前两行可以划掉

`courseWorkSubmissionsConnection` 和 `submissionStatistics` 原本是从 dashboard
扒下来的内部查询，一直以为只能配 cookie 会话。实测 Bearer 直接通过。
这意味着 M1 早报的跨课程 late/missing/excused 视图和每门课 Due/Missing/Submitted
计数，**全部可以在官方 token 下运行**，不需要浏览器。

注意这仍是**非文档**端点，Instructure 可以随时改。保留 REST 逐课 `assignments` 作为
慢速等价路径。

### 2. 两个 403 不是 token 的问题

报错文案是 `user not authorized to perform that action`，**不是** `insufficient scopes`。
区别要紧：

- `insufficient scopes` = token 被学校用 scoped developer key 收窄了 → 换 cookie 通道能救。
- `user not authorized` = **学生角色本来就没这个权限** → 换任何通道都一样打不开。

`page_views` 在 cookie 会话下同样是 403，与之前记录一致。`courses/:id/files` 是课程
文件索引，教师通常不对学生开放整个索引——但**作业附带的文件仍可通过
`assignments` 里的 URL 直接下载**，所以这条 403 对实际功能没有影响。

结论：**这两项上逆向通道没有任何优势，不必为它们保留代码。**

### 3. token 通道可以当主通道，逆向通道降级为纯备用

对比之前 cookie 会话的实测：

| | token | cookie 会话 |
|---|---|---|
| 有效期 | 长期（学校撤销为止） | 实测 10.3 小时后机器休眠中断，上限未知 |
| 轮换 | 无 | 每个请求 `Set-Cookie`，不跟上就死 |
| 首次设置 | 一条 `setx`，约 30 秒 | Playwright + SAML + Duo，人必须在场 |
| 依赖 | `httpx` | `httpx` + `playwright` + Chrome profile |
| GraphQL | 可用 | 可用 |

`session_monitor.py` 那个"会话到底能活多久"的未决问题，现在**不再挡路**——主通道
不依赖会话寿命了。这个问题降级为备用通道的已知限制。

## 对上层的影响

`ainl.canvas.ApiSource.from_env()` 只要看到 `CANVAS_TOKEN` 就自动走 Bearer，
所以早报、Telegram bot、L7-A 提交**已经在官方通道上了**，零改动。

跑到一半 token 死掉时的策略（已定，`ApiSource.recover_auth()`）：降级到 cookie 通道并在 Telegram 里
**明说**降级了——静默降级会让一个已撤销的 token 看起来和正常的一样。`/channel token` 钉住时不降级。
cookie 通道读的是开着的浏览器里的活 cookie；两边都没有会话时，bot 把 Canvas 登录页打开等学生登。
