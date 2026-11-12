[English](README.md) · **简体中文**

# AI-Native Learning (ainl)

一个架在 Canvas 以及课程转交出去的各家作业平台（WebAssign、Gradescope 等）之上的 Telegram 助手。
它找出要交什么、替你打开，并把**学生已经做完的作业**交上去，让学生把时间花在作业本身，而不是找按钮。

## 它做什么

- `/todo`：所有课程接下来几天要交的东西，每门课一块，每一项点一下就能打开。
- **Canvas 原生作业**（文字、文件、链接）：你确认后，通过 Canvas 官方 API 提交。
- **WebAssign**：bot 发来每道题的截图；选择题点按钮，其余写在纸上拍一张照，视觉模型**识别你的手写**
  （正常流程里唯一的一次模型调用）。你会在渲染好的截图上看到每个答案填进去的样子，不点 Submit 就不会提交。
- **Gradescope**：提交 PDF。
- 早报、截止提醒、会话保活，每一步都有耗时和指标记录。
- `scale-up/`：实验中的多学生版本（一个公开 bot，每人一份加密的 Canvas 会话）。

## 它刻意不做的事

**它从不生成答案。** 私有开发仓库里有一个只在测试时打开的开关，让语言模型扮演学生，以便在没人参与的情况下
跑通填写和提交流程。这个求解器不在本仓库里；`/auto` 和 `/sweep` 只会回复「不可用」。交上去的永远是学生自己的作业。

## 安装（简版）

Python 3.11+。

```sh
python -m venv .venv && .venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m playwright install chromium
```

密钥放在环境变量里（Windows 用户环境变量），或者 `~/.ainl/env`（macOS；模板见
`scripts/macos/env.example`），永远不进仓库：

| 变量 | 说明 |
|---|---|
| `CANVAS_TOKEN` | Canvas → Account → Settings → New Access Token（先确认你的学校允许个人 token） |
| `CANVAS_BASE_URL` | 你的 Canvas 地址，例如 `https://canvas.<school>.edu` |
| `TELEGRAM_BOT_TOKEN` | @BotFather 给的 |
| `TELEGRAM_CHAT_ID` | 运行 `python -m ainl.telegram --discover` 打印出来；bot 只服务这一个对话 |
| `GEMINI_API_KEY` | 识别手写（其他模型见 `src/ainl/mathinput/providers/`） |

然后运行 `python -m ainl.bot`。完整说明：[`docs/dev-setup.zh-CN.md`](docs/dev-setup.zh-CN.md)；
macOS 托管：[`docs/macos-hosting.zh-CN.md`](docs/macos-hosting.zh-CN.md)。

测试（离线）：

```sh
.venv/bin/python -m pytest tests canvas-official/tests scale-up/tests "Cookie Management/tests" -q
```

## 使用之前

- **非官方。** 与 Instructure（Canvas）、Cengage（WebAssign）、Gradescope 或任何大学都没有关联，也没有得到它们的认可。
  自动操作这些网站可能违反它们的服务条款或你所在学校的规定。两者都请自己确认；怎么用由你负责。
- **学术诚信是你自己的事。** 这个工具只会交你给它的东西。请只用在你自己完成的作业上，并遵守课程规则。
- **多学生模式会保管别人的 Canvas 会话。** 如果你替自己以外的人运行它，你就持有他们的凭据：
  `AINL_SESSION_KEY` 要保密，部署在你自己控制的机器上，并清楚告诉他们会存哪些东西。
- 各平台适配器是按一所大学的 Canvas 和课程设置（UW–Madison，2026 年秋季）写的，页面一改就可能失效。

## 目录结构

```
src/ainl/          bot 本体：canvas/（API + cookie 通道）、external/（WebAssign、Gradescope）、
                   mathinput/（手写 -> LaTeX -> MathML）、bot/、brief/、telegram/、util/
canvas-official/   只用官方 API 的最小客户端和提交探针
Cookie Management/ 每人一份的加密会话存储
scale-up/          多学生公开 bot（实验中）
scripts/           服务管理（macOS launchd、Windows）、探针、报告
docs/              架构、平台协议、研究笔记
tests/             离线测试，附少量手工制作的页面样本
```

## 文档语言

下表中的每份文档都有英文版（`X.md`，默认）和中文版（`X.zh-CN.md`），每份文档顶部的链接可以切换。

| 文档 | 内容 |
|---|---|
| [ARCHITECTURE.zh-CN.md](ARCHITECTURE.zh-CN.md) | 八层架构、提交分档、里程碑 |
| [ONBOARDING.zh-CN.md](ONBOARDING.zh-CN.md) | 60 秒上手目标、私聊还是群、多用户的责任 |
| [docs/dev-setup.zh-CN.md](docs/dev-setup.zh-CN.md) | 安装、密钥、两条 Canvas 通道、浏览器、bot 命令 |
| [docs/macos-hosting.zh-CN.md](docs/macos-hosting.zh-CN.md) | 在 Mac mini 上作为 launchd 服务运行、`/monitor` |
| [canvas-official/README.zh-CN.md](canvas-official/README.zh-CN.md) | 官方 API token 通道，以及 REST 做不到的事 |
| [Cookie Management/README.zh-CN.md](Cookie%20Management/README.zh-CN.md) | 多学生版本里每人一份的加密 Canvas 会话 |

`docs/` 和 `research/` 里的其余笔记（平台协议抓包、耗时分析、设计记录）是工作笔记，只有原始语言版本，大多是中文。

## License

MIT，见 [LICENSE](LICENSE)。
