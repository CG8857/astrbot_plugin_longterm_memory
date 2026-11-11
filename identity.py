"""平台无关的用户身份解析。

OneBot（aiocqhttp）的 sender_id 本身就是 QQ 号；QQ 官方机器人拿到的是
openid，因此还会尝试从消息内容中提取用户主动提供的 QQ 号。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

_QQ_RE = re.compile(
    r"(?:qq|QQ|扣扣|企鹅号|q号)\s*(?:号|号码)?\s*[:：是为]?\s*([1-9]\d{4,11})"
)


def extract_qq(text: str) -> str:
    if not text:
        return ""
    match = _QQ_RE.search(text)
    if match:
        return match.group(1)
    match = re.search(r"([1-9]\d{4,11})\s*(?:是|为)?\s*(?:我的)?\s*(?:qq|QQ)\b", text)
    return match.group(1) if match else ""


@dataclass(slots=True)
class UserIdentity:
    user_key: str
    platform: str
    platform_id: str
    platform_user_id: str
    nickname: str
    qq: str
    group_id: str
    session_id: str


def resolve_identity(event: Any) -> UserIdentity:
    platform = event.get_platform_name() or "unknown"
    try:
        platform_id = event.get_platform_id() or ""
    except Exception:  # noqa: BLE001
        platform_id = ""
    platform_user_id = event.get_sender_id() or ""
    nickname = event.get_sender_name() or ""
    if not platform_user_id:
        raw = f"{nickname}:{event.get_group_id()}:{platform_id}"
        platform_user_id = "anon_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]

    text = event.message_str or ""
    qq = ""
    if platform == "aiocqhttp" and platform_user_id.isdigit():
        qq = platform_user_id
    if not qq:
        qq = extract_qq(text)

    user_key = f"{platform}:{platform_id or 'default'}:{platform_user_id}"
    return UserIdentity(
        user_key=user_key,
        platform=platform,
        platform_id=platform_id,
        platform_user_id=platform_user_id,
        nickname=nickname,
        qq=qq,
        group_id=event.get_group_id() or "",
        session_id=event.unified_msg_origin or "",
    )
