"""Shared accessibility policy; application names alone never imply a composer."""
from doubao_typeless.core.policy import classify_focus
import re


def input_kind(role, description, *, editable, protected=False):
    if protected or 'password' in role.lower() or 'secure' in role.lower():
        return 'password'
    # 无障碍角色本身（如 AT-SPI 的 terminal）也是身份；不能只看名称/描述。
    special = classify_focus(description, role)
    if special in {'terminal', 'code'}:
        return special
    if not editable:
        return 'readonly'
    if any(term in description.casefold() for term in ('search', '搜索', 'history', '历史')):
        return 'edit'
    explicit = re.search(r'composer|chat[-_ ]?input|prompt[-_ ]?(?:input|textarea)|ask (?:anything|codex)|send a message|输入消息|输入你的问题', description, re.I)
    return 'composer' if explicit else 'edit'
