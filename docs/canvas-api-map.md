# Canvas API 逆向地图（canvas.wisc.edu，2026-09-15 实测）

数据源：Anything Analyzer 抓的 284 个 canvas.wisc.edu 请求（65 分钟真实浏览器会话），
加上 2026-09-14 直接用 session cookie 调 REST 的探测结果。

## 一、Canvas 前端自己用的三层接口

| 层 | 端点 | 用途 | 对我们 |
|---|---|---|---|
| **REST** | `/api/v1/*` | 公开文档化 API，Canvas 自己的页面也在用 | **主力**，有 Bearer token 就能调 |
| **GraphQL** | `POST /api/graphql` | 新版 Dashboard 全靠它 | **待办引擎**，比 REST 更贴近"我该做什么" |
| **页面内部** | `/courses/:id/modules/progressions`、`/files/pending` 等 | 非 API 的 JSON/表单端点 | 少数几个有用，其余别碰 |

## 二、认证：浏览器路径 vs. 机器人路径

### 浏览器（抓包所见）
```
Cookie:            canvas_session; _csrf_token; log_session_id
X-CSRF-Token:      <88 字符，来自 _csrf_token cookie 的 URL-decode>
X-Requested-With:  XMLHttpRequest
Accept:            application/json+canvas-string-ids, application/json
```
- `canvas_session` 是唯一的会话凭证，由 SAML + Duo 登录后签发
- 所有写操作（POST）必须带 `X-CSRF-Token`，值等于 `_csrf_token` cookie 解码后的内容
- **`application/json+canvas-string-ids`** 是个值得学的技巧：让服务端把所有 ID 以字符串返回，
  避免 JS 在 2^53 以上丢精度。我们的客户端也应该带这个 Accept

### 机器人（L0 该用的）
```
Authorization: Bearer <token>
```
- REST 与 GraphQL 都接受，**不需要 CSRF、不需要 cookie**
- 个人 token 从 `/profile/settings` 生成；OAuth2 developer key 走 `/login/oauth2/auth`
- 已验证：`/api/v1/users/self/profile`、`/courses`、`/assignments`、`/modules`、`/tabs`、`/external_tools`
  全部可用；`/users/self/page_views` 被 UW 管理员禁用（403）

## 三、REST 端点（按对我们的价值排序）

### ⭐ `GET /api/v1/planner/items` — 统一待办流，L1 的主数据源

```
?start_date=2026-09-01T05:00:00Z
&filter=incomplete_items
&order=asc&per_page=14
&context_codes[]=course_100002&context_codes[]=user_701304
```
返回**一条混合流**，四种类型一起来：

| plannable_type | 实测数 | 备注 |
|---|---|---|
| `assignment` | 14 | 带 `submissions.{submitted,excused,graded,late,missing,posted_at}` |
| `announcement` | 11 | `submissions: false`，只有 `read_state` |
| `quiz` | 4 | `html_url` 直接是 `/quizzes/743132` |
| `discussion_topic` | 4 | `html_url` 直接是 `/discussion_topics/…` |

每条的骨架：
```json
{
  "context_type": "Course", "course_id": "100002", "context_name": "SPANISH101…",
  "plannable_type": "assignment", "plannable_id": "3177849",
  "plannable_date": "2026-09-02T17:05:00Z",
  "plannable": { "id", "title", "due_at", "points_possible", "submission_types", … },
  "submissions": { "submitted": false, "graded": false, "missing": false, "late": false, … },
  "html_url": "/courses/100002/assignments/3177849",
  "new_activity": false, "planner_override": null
}
```

**为什么它是主数据源**：
1. `html_url` **已经是规范 URL**——quiz 给 `/quizzes/`，discussion 给 `/discussion_topics/`。
   这直接解决了 09-15 巡检发现的"assignment ID 会重定向"问题，L1 不用再自己跟随重定向。
2. `submissions.missing / late` 是 Canvas 算好的，L5 直接用
3. 公告和作业在同一条流里——正是"微信群里老师说一句"的数据形态
4. `context_codes[]` 可按课过滤，`filter=incomplete_items` 只拿没做完的

### ⭐ 三个让 `/todo` 从 12.8 秒降到 0.9 秒的参数（2026-09-20 补）

都是"过滤条件我们本来就有，只是用在了客户端"：

| 端点 | 参数 | 不带 | 带 |
|---|---|---|---|
| `GET /api/v1/planner/items` | **`end_date`** | 整学期 304 条 / 7 页 / 冷 6.5s | 窗口内 31 条 / 1 页 / 冷 0.76s |
| `GET /api/v1/courses/:id/assignments` | **`assignment_ids[]`** | 四门课 387 个（SPANISH101 一门 307 个）/ 10.3s | 31 个 / 1.5s |
| 同上 | `exclude_response_fields[]=description&…=rubric` | — | 响应体小一个数量级 |

各课程的请求彼此独立，可以并发（`httpx.Client` 线程安全）。
同一个 assignments 响应里还带 `external_tool_tag_attributes.url`——**它说明 external_tool 作业交给哪个供应商**
（`gateway.cengage.com/rest/launchBasicLTI/…` = WebAssign，`lti-gradescope.int.turnitin.com` = Gradescope），
不用开浏览器就能判断这项能不能在聊天里做。

### ⭐ `GET /api/v1/courses/:id/external_tools/sessionless_launch` — LTI 启动，官方端点

```
?assignment_id=<id>&launch_type=assessment      ← launch_type 必填，省略则 400
→ {"id": …, "name": …, "url": "https://canvas.wisc.edu/courses/:id/external_tools/sessionless_launch?verifier=…"}
```
**学生权限可用**（实测）。浏览器打开返回的 URL 即完成 LTI 启动并落到供应商的对应作业页——
不需要自己构造 OAuth 1.0 签名，也不需要 `form.target='_self'` 那个技巧。三个用途：

1. **Canvas 作业 → 供应商作业的权威映射**（落地 URL 里的 `dep=`），不必靠标题猜；
2. **不用供应商密码就能建立 / 重建供应商会话**——在零 cookie 的全新上下文里验证过；
3. 名字里的 "sessionless" 是字面意思：打开它**不需要 Canvas cookie**，`verifier` 自己就是认证。
   所以这个 URL **是凭据**，和 `UserPass=` 一样不能进日志。

### `GET /api/v1/users/self/favorites/courses?include[]=term&include[]=sections`
Dashboard 上展示的课（学生收藏的）。比 `/courses` 少噪音——那 10 门 orientation 课多半不在收藏里。
含 `calendar.ics`（每门课的 iCal 订阅地址）和 `time_zone`。

### `GET /api/v1/dashboard/dashboard_cards`
每张课程卡片。**注意 `links[]` 只是卡片上的 1~2 个快捷标签**，不是完整导航：
```
100003 MATH 222   defaultView=modules   links=[Announcements, Assignments]
100002 SPANISH101 defaultView=modules   links=[Announcements, Discussions]
```
完整导航要用 `GET /api/v1/courses/:id/tabs`（09-14 已验证，MATH 222 有 8 个、SPANISH 101 有 12 个）。
`defaultView` 告诉你老师把首页设成了什么（modules / wiki / assignments / syllabus）——L2 路由表的输入。

### `GET /api/v1/courses/:id/assignment_groups?include[]=assignments`
按分组（Homework / Quizzes / Exams…）带权重返回全部作业。`group_weight` + `points_possible`
= 算"这个作业占总成绩多少"的原料。L5 提醒的优先级可以按这个排。

### `GET /api/v1/courses/:id/students/submissions?per_page=50`
本人在该课所有作业的提交状态。字段：`workflow_state`（unsubmitted/submitted/graded）、
`cached_due_date`、`seconds_late`、`missing`、`late`、`score`、`grade`、`preview_url`。

### `GET /api/v1/courses/:id/module_item_sequence?asset_type=Assignment&asset_id=…`
给定一个条目，返回它在模块里的前后邻居——页面上 "◂ Previous / Next ▸" 就是它。
L4 拆解"这周要做什么"时可以顺着它走模块顺序。

### `GET /courses/:id/modules/progressions`（非 /api，但返回 JSON）
Canvas 自己的完成度追踪：每个模块的 `requirements_met[]`、`incomplete_requirements[]`、
`completed_at`、`workflow_state`。**这是学生"实际做到哪了"的真实来源**，比猜靠谱。

### `GET /api/v1/announcements?context_codes[]=course_X&start_date=…`
跨课公告流，带 `read_state` 和 `unread_count`。

### 噪音（浏览器会打，L0 不要模仿）
- `GET /api/v1/release_notes/unread_count` ×36 —— Canvas 版本更新提示
- `GET /api/v1/courses/:id/pages/account-settings` ×37 —— DesignPlus 主题插件的探测，全是 404
- `GET /api/v1/conversations/unread_count` —— 站内信角标
- `GET /api/v1/courses/:id/lti_apps/launch_definitions?placements[]=assignment_view` —— 全返回 `[]`

## 四、GraphQL（`POST /api/graphql`）

### 桥接模式
所有查询都通过 `legacyNode(_id: "701304", type: User)` 把 REST 的数字 ID 桥进 GraphQL：
```graphql
query { legacyNode(_id: $userId, type: User) { ... on User { … } } }
```
`type` 可以是 `User` / `Discussion` / `Course`。

### ⭐ `GetUserCourseWork` — Dashboard 的"Course work"面板

```graphql
query GetUserCourseWork(
  $userId: ID!, $first: Int, $after: String, $last: Int, $before: String,
  $courseFilter: String,               # "course_100003"
  $startDate: ISO8601DateTime, $endDate: ISO8601DateTime,
  $includeOverdue: Boolean, $includeNoDueDate: Boolean, $onlySubmitted: Boolean,
  $observedUserId: ID
) {
  legacyNode(_id: $userId, type: User) { ... on User {
    courseWorkSubmissionsConnection(first: $first, after: $after, courseFilter: $courseFilter,
        startDate: $startDate, endDate: $endDate, includeOverdue: $includeOverdue,
        includeNoDueDate: $includeNoDueDate, onlySubmitted: $onlySubmitted) {
      nodes {
        _id cachedDueDate submittedAt late missing excused state      # state: unsubmitted|submitted|graded…
        assignment { _id name dueAt pointsPossible htmlUrl submissionTypes state published
                     quiz { _id } discussion { _id } course { _id name } }
      }
      pageInfo { hasNextPage endCursor totalCount }
    }
  } }
}
```
实测变量：`{"first":6,"startDate":"2026-09-15T05:37:42Z","includeOverdue":false,"includeNoDueDate":false}`。
**`includeOverdue: true` 就能拿到已过期未交的**——那正是 L5 最该提醒的东西。
`assignment.quiz` / `assignment.discussion` 非空时，说明真身是 quiz / discussion（规范 URL 要换）。

### `GetUserCourseStatistics` — 每门课三个计数
```graphql
enrollments(currentOnly: true) { course { _id name
  submissionStatistics { submissionsDueCount(startDate:, endDate:) missingSubmissionsCount submissionsSubmittedCount }
  assignmentsConnection(filter: {submissionTypes: [online_quiz]}) { nodes { dueAt } }
} }
```
Dashboard 上 "48 Due / 0 Missing / 0 Submitted" 就是它。**一条查询拿全部课的计数**，早报的开头一句直接用。

### `GetUserAnnouncements` — 未读公告
```graphql
discussionParticipantsConnection(first:, filter: {isAnnouncement: true, readState: "unread"}) {
  nodes { id read discussionTopic { _id title message createdAt contextName contextId author { name avatarUrl } } }
}
```
`message` 是 HTML 全文——推进群里之前要转纯文本。

### 其余观察到的操作
| 操作 | 用途 | 对我们 |
|---|---|---|
| `GetDashboardNotifications` | 账户级通知 + 选课邀请 | 低 |
| `GetCourseInstructorsPaginated` | 各课教师姓名/邮箱 | L2 路由表（"找谁问"） |
| `GetCourseModulesForPeerReviews` | 模块里的互评作业 | 低 |
| `GetDiscussionQuery` / `GetStudentEntriesDiscussionQuery` | 讨论区内容分页 | L3 可选 |
| `GetPreferredLanguage` | 讨论翻译偏好 | 无 |
| `UpdateWidgetDashboardConfig`（mutation） | 记住 Dashboard 筛选 | 无 |

## 五、写路径（提交）
三步：`/files/pending` → inst-fs 上传 → `/courses/:id/assignments/:id/submissions`。
详见 `canvas-submit-protocol.md`。机器人用官方 REST 等价路径，带 Bearer，不需要 CSRF。

## 六、对各层的直接影响

| 层 | 决定 |
|---|---|
| **L0** | 主拉 `planner/items`（增量，按 `start_date`）+ `GetUserCourseStatistics`（计数）+ `GetUserAnnouncements`（未读）。Bearer 认证。带 `Accept: application/json+canvas-string-ids` |
| **L1** | `canonical_url` 直接取 `planner.html_url`，不再拼、不再跟随重定向。`state` 取 `submissions.{missing,late,submitted}` |
| **L2** | 完整导航用 `/courses/:id/tabs`，不用 `dashboard_cards.links`。`defaultView` 和 `GetCourseInstructorsPaginated` 进路由表 |
| **L4** | 模块内顺序用 `module_item_sequence`；实际完成度用 `modules/progressions` |
| **L5** | 死线来源 `cachedDueDate`；过期未交用 `GetUserCourseWork(includeOverdue: true)`；优先级用 `assignment_groups.group_weight` |

## 七、还没验证的
- Bearer token 是否能调 `/courses/:id/modules/progressions`（它不在 /api 下，可能只认 cookie）
- `planner/items` 的分页上限和 `per_page` 最大值
- GraphQL 的速率限制（REST 是 3000 req/h 的 leaky bucket，GraphQL 未见文档）
