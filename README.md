<div align="center">

# Pocket Composer
### 手机说清楚，圈出来，画明白。电脑接着做。
**手机到电脑的图文输入工具 · Windows 正式版 / macOS、Linux 体验版**

[English](README.en.md) · [下载稳定版](https://github.com/aaakoako/Pocket-Composer/releases/latest) · [安装指南](docs/release/v3-installation.md) · [反馈](https://github.com/aaakoako/Pocket-Composer/issues)

![Windows](https://img.shields.io/badge/Windows-10%20%2F%2011-5b5ce2) [![MIT](https://img.shields.io/badge/License-MIT-6254e8)](LICENSE)

</div>

向 Agent 描述界面问题、解释代码需求或画一个布局时，手机往往比键盘更顺手。Pocket Composer 把手机输入法、截图标注和白板放在一起，将整理好的图片与文字插入 Windows 上的目标输入框。

**Windows 稳定版：0.5.9。Mac/Linux 仍为体验版。** [发布说明](docs/release/0.5.9-network-update-install.md)

<table><tr><th>手机负责表达</th><th>电脑接着做</th></tr><tr><td align="center"><img src="docs/images/product/phone.png" width="270" alt="0.5.5候选手机图文输入与同步" /></td><td align="center"><img src="docs/images/product/hud.png" width="430" alt="0.5.5候选正文优先的电脑浮窗" /><br/><br/>查看、修改、复制、插入。<br/>辅助参考按需展开。</td></tr></table>

## 说清楚，也能画明白

| 你想做什么 | 用哪个入口 |
|---|---|
| 快速说一段需求 | 手机输入法语音输入，仍可手动改字 |
| 指出屏幕上的问题 | **截电脑** → 圈画、箭头、编号、图注 |
| 带上参考图片 | **相册** → 编辑、排序、添加说明 |
| 表达一个布局或想法 | **白板** → 手绘、图形、文字 |
| 放进电脑输入框 | **插入并复制** → 逐图后接文字，默认不发送消息 |
| 继续下一段或找回上一份 | 清空/撤销当前稿，恢复最近图文 |

<div align="center"><img src="docs/images/product/phone-workflow.gif" width="300" alt="真实手机页面录制：输入、白板与同步" /></div>

*实际页面、演示内容；展示编辑与同步，不冒充目标输入框接收结果。语音识别由手机输入法提供。*

## 三步开始

1. **打开电脑客户端**，让手机与电脑接入可互通的可信网络，手机扫码配对。
2. **允许插入并选中目标输入框**，例如你正在使用的 Agent 对话框。
3. **手机输入或加图，点“插入并复制”**。检查电脑里的内容后，再自行发送。

不需要启用模型服务。失败时原稿保留，可复制、手动粘贴或恢复。独立的手机发送功能默认关闭，需要电脑授权和手机明确确认。

## Jev：可选的输入参考

**默认关闭，不影响输入、图片、复制和插入。** 开启后可查看疑似转写问题、四个独立参考维度和文字语气。不打总分、不预测成功率、不强制追问。正文右侧用短标签和音量条式分段显示状态；关闭后侧栏完全收起。完整详情按需展开。

<div align="center"><img src="docs/images/product/input-feedback.gif" width="430" alt="可选语气图标、分段参考侧栏与火焰彩蛋" /></div>

*实际 Qt 界面录制，参考与语气为合成示例。火焰是界面彩蛋，不是对真实心理状态的判断。*

Jev 支持 TypeSafe、OpenRouter、Vercel 和兼容自定义接口；纠错/改写模型单独配置。自备 Key 和额度，费用由服务商收取。仅参考当前文字，**不读取目标 Agent 历史或执行结果**。应用动效可单独关闭。

## 下载、数据与兼容性

| 电脑系统 | 下载与状态 |
|---|---|
| Windows 10 / 11 | [稳定版](https://github.com/aaakoako/Pocket-Composer/releases/latest) / [0.5.5 体验版](https://github.com/aaakoako/Pocket-Composer/releases/tag/preview-0.5.5) |
| macOS · Apple Silicon / Intel | [DMG 体验包](https://github.com/aaakoako/Pocket-Composer/releases/tag/preview-0.5.5-posix)，需辅助功能权限；尚未 Apple 公证 |
| Linux x64 · X11 | [DEB / tar.gz 体验包](https://github.com/aaakoako/Pocket-Composer/releases/tag/preview-0.5.5-posix)，Ubuntu 22.04+；Wayland 自动插入暂不支持 |

[macOS / Linux 安装、权限与限制](docs/release/CROSS_PLATFORM.md)。跨平台体验包不代表已完成真实 Codex/Cursor 与实体手机验收。

- [Releases](https://github.com/aaakoako/Pocket-Composer/releases)提供当前用户安装包和完整便携包；便携包须整体解压。仓库已更名为 Pocket-Composer，旧版内置自动更新不再维护，请从新仓库手动下载。安装文件和已有数据目录暂沿用 DoubaoTypeless 标识，保留现有草稿。
- 手机使用浏览器，电脑面向 Windows 10/11。浏览器、输入法和目标应用的兼容性以实际体验为准。
- 已收到 Codex 桌面连续图文插入的用户反馈；**不保证所有输入框都可自动识别**。Cursor、多屏和实体手机输入法仍需进一步验证。
- 草稿、图片和设置本地保存。支持断线续写和重连核对，升级保留工作区，卸载不主动删除草稿。
- 手机桥接用于可信网络；启用模型功能后，相应文字会发送给所选服务商。

[安装、升级与数据](docs/release/v3-installation.md) · [已知限制](docs/release/KNOWN_LIMITS.md) · [隐私说明](docs/PRIVACY.md)

## 开源与商用

[MIT](LICENSE) 允许商业使用、修改和再分发，须保留版权与许可声明。第三方组件保留各自许可证；模型服务有独立条款和费用。

[商用条件 · 中文 / English](docs/legal/COMMERCIAL_USE.md) · [第三方声明](THIRD_PARTY_NOTICES.md)

Pocket Composer 是独立项目，不声称与豆包、字节跳动、OpenAI、Cursor 或模型服务商存在官方合作。

## 参与项目

[反馈问题](https://github.com/aaakoako/Pocket-Composer/issues)时，请附版本、浏览器/输入法、复现步骤与脱敏截图；不要上传 Key、配对凭据或私人正文。

[构建与验证](docs/release/v3-build.md) · [更新记录](CHANGELOG.md) · [旧版指南](docs/legacy-v0.4.md)

