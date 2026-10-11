"""统一插入反馈文案；不把输入事件发出称为目标已接收。"""
ERRORS = {
    'ACCESSIBILITY_REQUIRED': '请开启系统辅助功能权限并选中输入框；内容已保留，可复制后粘贴',
    'X11_REQUIRED': '自动插入需要 X11 桌面会话；内容已保留，可复制后粘贴',
    "COMPOSER_AMBIGUOUS": "当前窗口有多个对话输入框，未自动选择；请点击需要的输入框",
    "COMPOSER_NOT_FOUND": "没有唯一可确认的对话输入框，未移动或插入；可手动点中目标",
    'PHONE_OFFLINE': '手机已离线，内容保留；连接恢复后再插入',
    'PHONE_NOT_CURRENT': '未拿到手机最新内容，未插入旧稿；请重连后重试',
    'PHONE_CHANGED_REVIEW': '手机稿已更新，电脑修改保留；请展开对比',
    'CONNECTION_PAUSED': '连接已暂停，先从托盘恢复连接',
    'EMPTY_DRAFT': '还没有可插入的内容',
    'IMAGE_EDITING': '图片尚未完成，请在手机完成编辑或重试上传',
    'SOURCE_NOT_RENDERED': '截图尚未处理完成，未插入原图',
    'ASSET_MISSING': '图片文件缺失，当前图文已保留',
    'TARGET_CHANGED': '输入目标变化，本次已停止；请点回目标再操作',
    'NEEDS_TARGET': '请先点中目标输入框；当前内容已保留',
    'OWN_WINDOW': '请先点中其他应用的输入框，再按插入快捷键',
    'MODIFIERS_HELD': '快捷键仍按住，请松开后再试',
    'SESSION_LOCKED': '桌面已锁定，没有插入',
    'TARGET_ELEVATED': '目标以更高权限运行，未插入；可复制后手动粘贴',
    'TARGET_PERMISSION_UNKNOWN': '无法确认目标权限，未插入；可复制后手动粘贴',
    'TARGET_INSPECTION_TIMEOUT': '输入框检查超时，内容保留；可重试或复制后手动粘贴',
    'TARGET_INSPECTION_FAILED': '输入框检查中断，主程序仍可用；可重试或复制',
    'INPUT_REJECTED': 'Windows未接受按键，内容保留；可复制后手动粘贴',
    'INPUT_PARTIAL': 'Windows只接受部分按键，请先检查目标；没有自动重贴',
    'CLIPBOARD_INTERFERENCE': '剪贴板被其他操作改变，已停止插入且未覆盖它',
    'CLIPBOARD_UNAVAILABLE': '剪贴板暂时读不出，没有继续插入；原稿保留，可重试',
    'DELIVERY_FAILED': '插入未完成，内容保留；可复制或重新选择目标',
    'FINALIZE_FAILED': '插入结果待确认，恢复副本已保留；请先检查目标',
    'COMMAND_FAILED': '本次操作中断，内容保留；程序仍可继续使用',
    'DUPLICATE_INTENT': '这次请求已经处理，未重复插入；上次结果可恢复',
    'COPY_FAILED': '剪贴板暂不可写，未复制；正文和图片仍保留',
    'BUSY': '正在处理上一次操作，没有排队重复插入',
    'SHUTTING_DOWN': '正在退出，未接受新的插入',
}


def error_message(payload: dict) -> str:
    code = payload.get('detail_code') or payload.get('error_code') or 'DELIVERY_FAILED'
    message = ERRORS.get(code, ERRORS['DELIVERY_FAILED'])
    progress = payload.get("progress") or {}
    if progress.get("images_attempted") and progress.get("text_state") == "not_attempted":
        message += "；图片已尝试插入，文字尚未插入且仍保留"
    return message
