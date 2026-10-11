# 0.5.9 本机插入与同步故障修复

运行中的日用版为 `f1632ed8e1df0cc8b434b8bc1619731908760221` / 0.5.9；实际修复目录是 `G:/AgentStorage/Workspaces/DoubaoTypeless-input-sync-20261011`，分支 `fix/input-sync-20261011`。用户手机使用安卓浏览器与豆包输入法。

## 原因与修改

- 日用日志显示多次 `CLIPBOARD_INTERFERENCE`、`steps=0`，尚未发出粘贴。后台原生实测发现写入后立即 `GetClipboardData` 会返回 Windows 1418，10 ms 后同样的正文可读。旧版只重试打开剪贴板，读取失败被 `V3App._read_clipboard_text` 吞成 `None`，进而误报内容被改变。现在在 250 ms 内重试整个读取事务；真实内容不一致仍停止，持续不可读显示独立错误，且不以备用复制覆盖它。
- 手机页仅监听 `input`，漏发事件的输入法尾段不会进入状态/持久化/同步。加入合成结束、失焦、change 及前台每 200 ms 的轻量核对；渲染、插入与处理清空回执前也核对。程序清空或恢复以新状态为准，避免旧 DOM 复活；未同步的新文字会使旧清空回执失效。
- 手机把失败请求的 ID 留在同一稿里，导致再次点击仍被判重。现在仅对带错误且明确 `steps=[]` 的终态允许下一次用户点击使用新请求；已有步骤、结果丢失或重复回执继续判重，没有自动重放。
- 自动清空原先被真正的零步骤失败阻止，属于连带症状。保留既有规则：完整粘贴步骤可进入下一段、保留上次恢复副本；系统发键证据仍不等于外部目标确认收到。

## 已验证与范围

- TypeScript 类型检查与 Vite 生产构建通过；106 项相关回归及 8 项新增生产页面检查通过。新增检查覆盖漏事件全文、手机插入后清空/下一段/恢复、旧回执保留新尾段、电脑准备请求、读取失败后不改字再次点击。
- 原生 Windows 后台实测：20 轮真实剪贴板与隐藏 EDIT 输入框，18 次瞬时读取错误在重试后恢复，20 次正文完整，无可见窗口或前台切换；原剪贴板恢复。隐藏 EDIT 用 `WM_PASTE`，未声称验证真实 Ctrl+V/热键/用户目标。
- 旧安装包的生产网页与修复网页对比，在同一隔离桥接服务上模拟缺失 input：旧版手机 24 字符、电脑仅 4；修复版两端均 24。不等于安卓豆包输入法真机测试。
- 证据在 `docs/v3-delivery/evidence/input-sync-20261011/`。网页测试使用真实 Chromium、WebSocket 和 IndexedDB；按键投递用替身。日用目录、配置、配对与历史未被修复代码覆盖，运行中的日用进程没有重启。

尚待修复候选的冻结启动检查；实体安卓手机与真实 Codex/Cursor 插入、日用版切换及用户验收尚未完成。候选不发布、不合并主干。

Windows 读取/写入契约参考：[GetClipboardData](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getclipboarddata)、[SetClipboardData](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setclipboarddata)。本机 1418 与恢复结论来自实测，非文档推断。
