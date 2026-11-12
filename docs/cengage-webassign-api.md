# Cengage / WebAssign 逆向地图（MATH 222，2026-09-16 实测，付费墙已打开）

数据来源：真实 Chrome 走完整链路（DOM + 内联表单 + 外部脚本 grep），
外加 Tampermonkey flow-events-2026-09-17.json（547 事件 / 21 域名）交叉验证。
**未提交任何答案做代做**——写路径从按钮绑定和表单结构推断；提交端点的 POST body 仍需 MITM。

## 一、启动链路（LTI 1.1）

```
canvas.wisc.edu/courses/100003/assignments/3152478
  └─ form POST (target=_blank → 改成 _self 绕弹窗拦截)
     https://gateway.cengage.com/rest/launchBasicLTI/500001/6000000000000000000000000001/500002/activityId/EXAMPLEACTIVITYID0001
       → student.cengage.com/dashboard/
       → www.cengage.com/dashboard/enrolled-course/WA-production-700002?…   ← 付费前停这（PAYMENT REQUIRED）
       → www.webassign.net/web/Student/Assignment-Responses/last?dep=40575435  ← 付费后直达作业
```

- `activityId/EXAMPLEACTIVITYID0001` = Canvas 作业 ↔ WebAssign 作业的稳定映射键
- `WA-production-700002` = Cengage 课程 id；`course=700001,700002` = 两个 section
- WebAssign 用户 id：`90000001`
- **access_state 判定**：落地停在 `cengage.com/dashboard/enrolled-course` + 卡片 PAYMENT REQUIRED → `payment_required`；
  到达 `webassign.net/web/Student/…` → `ok`

## 二、"我的作业"列表 = WebAssign 侧 planner

导航 **My Assignments** → `POST https://www.webassign.net/v4cgi/student.pl`（旧 Perl CGI + React 壳）。
隐藏字段：`CSRFToken`, `UserPass`, `userId=90000001`, `course=700001,700002`, `action=home/index`,
`struct/savedStruct/subaction`（路由）, `modalPurchaseStruct`, `modalHomeStruct`。

**实测 49 个作业**，每行 `[dep, 标题, 类型, 到期, 分]`：
- 类型两种：`Homework`（/10，周四到期）、`Module quiz`（/2，周日到期）
- `dep` 连号（40575434…40575467+），按发布顺序，非到期顺序
- 到期统一 23:59，11 月由 CDT 转 CST
- **每个作业规范 URL = `/web/Student/Assignment-Responses/last?dep=<dep>`** ← L7-C 深链
- 头几个：40575435 (7.2 HW), 40575436 (7.3 HW), 40575437 (MQ9), 40575434 (MQ6)…

Canvas 报 48 项 vs WebAssign 49 项：差 1，待核（可能 practice 或未发布项）。

## 三、作业页结构（`Assignment-Responses/last?dep=…`）

```
<form>  hidden: course, openStudentView=0, action=home/index, UserPass, CSRFToken, deployment=<dep>, user=90000001
        hidden: RA_<qid>_<qIdx>_<partIdx>_<respId>  +  enabled_RA_…   ← 每答题部件一对
  div#question<qid>_<qIdx>.waQBox     ← 每题一容器
     textarea ×N（MathType 输入）
     input[submit] name=submit    onclick="$('question<qid>_<qIdx>').retrieve('question').submit(true);return false"
     input[submit] name=save      onclick="… .save();return false"
     input[submit] name=SubmitAll onclick="… .submitAll();return false"
```
实测 10 题，例：`question4596595_0` → parts `RA_4596595_0_{0..3}_4604775`（4×0.25 分）。
- `qid`=题库题目 id；`respId`=本部署响应槽 id
- 三个按钮都 `return false`，走 **MooTools** `question` 对象（不是原生表单提交）
- 前端栈：MooTools 1.3.2 + jQuery 1.7.1 + Angular 1.2.14 + Dojo 1.6.1 + React（五代并存）
- 数学输入：WIRIS MathType 7.23.3（`/mathtype/editor/…`），符号面板 = 图中那个 Math Pad

## 四、已知 REST 端点（脚本 grep）

| 路径 | 用途 |
|---|---|
| `GET /web/Rest/Users/cmp_expiration` | **访问到期探测 → access_state 该轮询它** |
| `/web/Rest/AYT/{read_messages,submit_message,answer_messages,messages_for_question}` | Ask-Your-Teacher 问答 |
| `/web/restricted-features/user-settings` | |
| `/web/Rest/Ebook-Vitalsource/launch` | 电子书（VitalSource） |
| `POST /v4cgi/student.pl` | 列表 / 路由（旧核心） |
| `POST /mathtype/editor/tick` | MathType 心跳 |

## 五、Tampermonkey 覆盖确认（547 事件 / 21 域名，四门课）

四门课 Canvas 活动全记录；所有 `leavesTo` 目标落地时也都记到，**零覆盖缺口**。
WebAssign 一整道题 + Submit 交互链完整（Math Pad 符号 → Save Progress → 22:54 Submit Answer）。
结论：记录器规则正确，**到主站后所有转跳链都跟得住**，可复制给同学采集。

**局限（决定选型）**：Tampermonkey 记的是**点击标签**，不是**网络请求**——证明了"点了 Submit"，
但拿不到那次 POST 的 URL/body。要提交端点只能靠 MITM。两工具互补，非替代。

## 六、对各层的落地

| 层 | 决定 |
|---|---|
| L1 | MATH222 每个 external_tool 作业补 `external_host=webassign.net`、`external_id=<dep>`；靠标题精确匹配或 activityId |
| L2 | route: `homework.submit = {tier:C, target:webassign, deep_link:/web/Student/Assignment-Responses/last?dep=<dep>}` |
| L5 | access_state 轮询：跑一次 LTI 看落地域名，或 `GET webassign.net/web/Rest/Users/cmp_expiration` |
| L7-C | 深链 = 改 target=_self 提交 Canvas LTI 表单（保签名、绕弹窗）。**不做 L7-B 代交**（计分测评 + 次数有限） |

## 七、为什么 Anything Analyzer 内嵌浏览器进不去 Cengage（2026-09-17 定论，修正早前猜测）

**不是签名问题。是缓存导致的 webpack 代码块 404。** 证据（analyzer DB，embedded session）：
- 对 `gateway.cengage.com` **零 POST**——LTI 启动表单从未提交
- 仅有的 cengage 请求是 3 个 GET：`778/34/42.app.bundle.js?v=1.40.2-SNAPSHOT`，全部 **404 Apache "Server Error"**
- 之后立即 302 弹回 canvas.wisc.edu

第二次会话（2026-09-17 "cengage and webassign"，70 s）复现并**否定了缓存假说**：
- 版本号已变（`v=1.40.2` → `v=1.41.0-SNAPSHOT`），外壳是新的，仍然 404
- 整个会话没有任何 gateway.cengage.com 的 HTML 文档请求 → 代码块是从 **Canvas 作业页内嵌的 iframe** 里请求的
- 请求的是**根路径** `/778.app.bundle.js`，而 Cengage 前端实际部署在 `/frontend/`（错误页 URL 为证）

机理（当前最佳解释）：webpack 5 `publicPath:'auto'` 在运行时用 `document.currentScript.src` 推算资源根路径。
在 analyzer 内嵌浏览器的 iframe 里，其脚本注入/指纹层导致 `currentScript` 不可用 → 回退为 `/` → 根路径 404 →
LTI 启动脚本从未执行 → 签名 POST 从未发出。真实 Chrome 中 `currentScript` 正常 → `/frontend/` → 正常。
**换版本不好、清缓存不好——问题在 JS 运行时环境，不在网络层。** 内嵌浏览器不是这活儿的工具。

**修正**：早前 the author's private traffic-capture notes (not included) 里"Signature Verification = analyzer CDP 破坏 OAuth 签名"
的结论作废。那个 `frontend/error?...Signature+Verification` 出现在真实 Chrome、买书之前，是 Cengage 未注册态，
购书后消失，与 analyzer 无关。

对 analyzer 的可行修复（若非要用它）：清空内嵌浏览器缓存/storage 后重进。但不建议——见第八节，有更好的路。

## 八、拿 WebAssign 提交内部编码：工具与方案

需要的数据分两种，别混：
1. **答案输入语法**（往答题框里键入什么，如 `(1/16)*(x - sin(16*x)/16) + C`）——**M2-M 近期版需要的就是这个**，
   可直接从页面 DOM / 试填得到，**不需要抓 POST**。
2. **提交 POST 的 URL + body（RA_* 字段编码）**——只有做**全headless 代交**才需要，属已约定推迟的 L7-B。

**claude-in-chrome 拿不到 #2**：它的 read_network_requests 只稳定给 url/method/status/headers，
不给 request body。这是"我自己拿不到、需要你配合"的那块。

抓 #2 的工具选型：

| 方案 | 装什么 | 能读 body | 破坏签名? | 我能否直接读 |
|---|---|---|---|---|
| **Reqable（已连 MCP）** ✅ 推荐 | 一次性装 CA 证书 | ✅ 全量 req+resp | 否（透明 MITM 转发原字节） | ✅ 经 MCP `capture_live_get_by_id` 直接读，无需导出 |
| Anything Analyzer MITM + 真实 Chrome | 系统代理 + 证书 | ✅ | 否 | 经其 MCP |
| 用户脚本 hook XHR/form | 无 | ✅（仅本页出站） | 否 | 需你导出 JSON |

Reqable 最优：已安装已连 MCP、真实 Chrome（Cengage 能跑）、我直接读到 body+响应。

## 九、WebAssign 答案内部编码与填写接口（2026-09-17，真实 Chrome 只读实测，零抓包）

### 编码 = MathML（WIRIS MathType 方言）
每个答题部件一对隐藏字段：`RA_<qid>_<qIdx>_<part>_<respId>`（MathML 字符串）+ `enabled_RA_…`（`1`）。
已提交答案原文（7.2 HW，Q1 四个小题，全对，每题 0.25/0.25）：

| part | RA_ 值（去掉外层 `<math xmlns="http://www.w3.org/1998/Math/MathML"><mspace/>…</math>`） | 含义 |
|---|---|---|
| 0 | `<mfrac><mn>1</mn><mn>16</mn></mfrac><mi>sin</mi><mfenced separators=""><mrow><mn>16</mn><mi>x</mi></mrow></mfenced>` | (1/16)sin(16x) |
| 1, 2 | `<mn>0</mn>` | 0 |
| 3 | `<mn>0</mn><mo>.</mo><mn>5</mn><mi>&#x3C0;</mi>` | 0.5π |
| Q2 p0 | `<mn>4</mn><mi>sec</mi><msup><mfenced><mrow><mn>4</mn><mi>x</mi></mrow></mfenced><mn>2</mn></msup>` | 4sec(4x)² |

方言特征：开头固定 `<mspace/>`；函数名用 `<mi>sin</mi>` 而非 `<mo>`；括号用 `<mfenced>`（有时带 `separators=""`）；
π 用 `<mi>&#x3C0;</mi>`；小数写成 `<mn>0</mn><mo>.</mo><mn>5</mn>`；未作答 = `<math…><mspace/></math>` 或空串。

### 可见编辑器
`div.mathtype-wrapper` 内嵌 WIRIS 编辑器；旁有隐藏 `textarea#latex-source-RA_…`（class `latex-source`，平时为空）。
页面全局：`LaTeX2HTML5`、`com.wiris.{js,jsEditor,editor,formula}`。

### 页面暴露的接口 —— 以及哪些**不能**用来填（2026-09-19 在活页面上读源码修正）

```js
setMathTypeValue(editorType, mathml, cb)      // → mathTypeEditor[editorType].setMathMLWithCallback(...)
window.mathTypeOverlay.syncAnswerValue(boxId) // ⚠️ 方向是 编辑器 → 隐藏字段，不是反过来
window.mathTypeOverlay.renderPreview(boxId)   // 用隐藏字段的值重画可见的答案框
const q = $('question<qid>_<idx>').retrieve('question')
q.save()          // = Save Progress，不消耗提交次数
q.submit(true)    // = SUBMIT ANSWER 按钮的 onclick，原样
```

> **本节初版是错的。** 它写"`setMathTypeValue` 之后调 `syncAnswerValue(boxId)` 把内容写回字段"。
> 读了 `syncAnswerValue` 的源码才发现：它把**共享的那一个 WIRIS 编辑器**的当前内容倒灌回字段；
> 编辑器没挂在这个框上时内容为空，于是它会把**刚填好的答案清成空**。
> 函数名叫"同步答案值"，数据流方向却和我们要的相反——**只看函数名不读源码，就会得到这个错误**。

**正确的填入（已在真实作业上验证）：**

```js
field.value = mathml                                  // 直接写隐藏字段
// 触发 change —— 不能用 new Event('change')：MooTools 1.3 把全局 Event 类整个替换了，
// 它的构造函数期待一个真实事件，传字符串会抛异常
const ev = document.createEvent('HTMLEvents'); ev.initEvent('change', true, false)
field.dispatchEvent(ev)
window.mathTypeOverlay.renderPreview(boxId)           // 从字段重画，全程不碰编辑器
```

**提交**不调 `q.submit(true)`，而是**点那道题自己的 SUBMIT ANSWER 按钮**。按钮的 onclick 就是那一句，
点它走的是和学生手点完全相同的路径，WebAssign 以后在按钮外加什么逻辑我们都自动跟上。
（MooTools 的类方法有 `$protected` 检查，从外部直接调也可能抛 "cannot be called"。）

### 字段命名（2026-09-19 普查全部 10 份作业）

```
RA_<qid>_<qIdx>_<part>_<respId>   Answer  答案框：旁边有 #editable-math-<name> 就是数学框，没有就是纯文本
RC_<qid>_<qIdx>_<part>_<respId>   Choice  单选（radio）
RS_<qid>_<qIdx>_<part>_<respId>   Select  多选（checkbox）
```
`qIdx` = 页面上的题号 − 1。题目容器是 `#question<qid>_<qIdx>`。下拉匹配题用普通 `<select>`。

题型分布：math 35、choice 9、text 2、dropdown 1、multi 1、unknown 1。
**分类单位是答案部件不是题**：7.8 Homework 的第 2–4 题各有一个单选（convergent / divergent）
**外加**一个数学框。Module Quiz 12 的选项是图片（方向场），label 为空。
选中单选要 `.click()` 而不是设 `.checked`——这些 input 上挂着 MooTools 处理器。
普查脚本：`scripts/wa_survey.py`；实现：`src/ainl/external/webassign.py`。

### 次数与计分（实测）
每个 part 独立计分，**每个 part 20 次提交机会**，"Your last submission is used for your score"——最后一次为准。
填入不消耗次数（"the number of submissions remaining only changes if you submit"）。
MATH222 的 **Module Quiz 不限时、不限次数**（学生确认 + 页面上无计时器/次数字样，2026-09-19）；
其它带 "quiz" 的一律先问再开。**没有撤回**：想"撤回"就是重新提交一次正确的。

### 管线（现状）
```
/todo → 回复编号 → 每题一张截图（相册）
  选择 / 多选 / 下拉 → Telegram 按钮点选（0 次模型调用）
  数学框            → 学生一张纸写完所有题、标题号、拍一张 → Gemini Flash-Lite 读一次
                      → 按题号切分；一题多空用逗号分隔，按空的出现顺序对应
  → LaTeX → WIRIS 方言 MathML → 写入 RA_ 字段 + renderPreview
  → 渲染后的截图回传 → [✅ Submit] [✏️ Modify] [✖ Cancel]
  → 学生点 Submit 才逐题点 SUBMIT ANSWER，读回得分
```
改正一道题优先**打字**（0 次调用）；拍单题是 1 次调用。

## 十、会话：Cengage 的登录撑不过浏览器重启，但不需要密码

真正代表登录的 cookie 是 `www.webassign.net` 的 **`JSESSIONID`** 和 cengage.com 的 **`cmp-session-id`**，
**都是会话级的**，浏览器进程一关就没；那几十个持久 cookie 全是统计类。所以**每次重启浏览器都会登出**。
（Chromium 被强杀后有时会靠崩溃恢复还原会话 cookie——不可靠，别指望。）

重建靠 Canvas，不靠 Cengage 密码：

```
GET /api/v1/courses/<course>/external_tools/sessionless_launch
      ?assignment_id=<canvas assignment id>&launch_type=assessment
→ {"url": "https://canvas.wisc.edu/.../sessionless_launch?verifier=…"}   ← 学生权限可用（实测）
浏览器打开该 URL → 自动提交 LTI 表单 → 落在 /web/Student/Assignment-Responses/last?dep=<dep>
```

在**零 cookie 的全新浏览器上下文**里验证过：事前 0 个 cookie，7.2 秒后落到对应作业页（24 个题目区块、
8 个答案框），Cengage / WebAssign 的会话 cookie 都是这次启动现场建立的。
所以新用户只需登 Canvas；这个 launch 同时给出了 Canvas 作业 → `dep` 的**权威映射**（标题匹配只是退路）。

- `verifier=` 的 URL **本身就是凭据**（无需任何 cookie 就能换到会话），与 `UserPass=` 一样，日志里必须脱敏。
- 该页会自动提交表单，打断正在进行的导航并抛 "interrupted by another navigation"——那是**成功**的信号。
- 同一学生**不要并发两个上下文**登 Cengage，会互相顶掉。

## 十一、还没拿到的

- 分步引导题（7.4 第 4 题，`SCalc9 7.4.023.MI.SA`，"This question has several parts"）的结构——目前分类为 unknown
- `question.submit()` POST 的 URL 与 body（走按钮点击后不再需要）
- Past/All Assignments tab 请求形状 · Grades 页结构 · `cmp_expiration` 返回体


## 12. 保存（不提交）——2026-09-21 实测

- 每道题有一个隐藏的 `Save Progress` 按钮（`input#save_<qid>_<idx>`，class 带 `hide`），onclick 是 `$('question…').retrieve('question').save()`。页面本身也在自动保存（显示 "Viewing Saved Work"）。
- `save()` POST 到 `/web/Rest/Response/save`，表单字段 `deployment`、`user`、`responses`、`allResponses`、`smw`；`responses` **只含页面认为改过的部件**，没有改动时这个字段整个不出现。返回 200 + 一段 `live_update` XML。
- **必须逐题：写完一道题的所有部件 → 立刻 `save()` 这道题。** 先把整页写完再逐题 `save()`，刷新后每题只剩一个部件。
- 保存不消耗 submission；刷新、换浏览器后仍在。这是 `/resume` 的基础。
- 同一会话里开**第二个标签页**加载同一份作业有时会卡在 about:blank；核对服务器状态用刷新原标签页。
- `mathTypeOverlay.renderPreview(boxId)` 防抖约 0.3 s；新加载页面上第一次调用会被丢弃，第二次才画。判断画好了要比对 `.mtAnswer .MathJax[data-mathml]` 与隐藏字段的值，不能只看宽度。
