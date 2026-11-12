# Flow Recorder

记录你在 Canvas 及其外部跳转站点上的真实点击路径，用来量化「交一次作业到底要跨几个域名、点几下」。

## 安装

1. Chrome 装 [Tampermonkey](https://chromewebstore.google.com/detail/tampermonkey/dhdgffkkebhmkfjojejmpbldmpobfkfo)
2. Tampermonkey → 管理面板 → Utilities → Import from file → 选 `flow-recorder.user.js`
3. 确认已启用（图标上有计数）

## 使用

正常用 Canvas 就行，不需要任何操作。

Tampermonkey 菜单（点扩展图标）里有三个命令：
- **stats** — 看目前记了多少、跨域跳转几次、各域名分布
- **export JSON** — 导出成文件
- **clear** — 清空

## 隐私

- 只记录：URL、链接/按钮的**文字标签**、域名跳转、文件名+大小、页面停留秒数
- 不记录：页面正文、答案、成绩、私信、任何表单填写的值、密码
- 不联网。数据存在 Tampermonkey 本地存储里，只有你导出时才会离开浏览器。
