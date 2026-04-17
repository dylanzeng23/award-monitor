"""Lightweight WeChat (iLink) message sender for award alerts."""

import base64
import json
import logging
import secrets
import struct
import uuid

import httpx

logger = logging.getLogger(__name__)

ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
EP_SEND_MESSAGE = "ilink/bot/sendmessage"
CHANNEL_VERSION = "2.2.0"
ILINK_APP_ID = "bot"
ILINK_APP_CLIENT_VERSION = (2 << 16) | (2 << 8) | 0

# Message constants
MSG_TYPE_BOT = 2
MSG_STATE_FINISH = 2
ITEM_TEXT = 1


def _random_wechat_uin() -> str:
    value = struct.unpack(">I", secrets.token_bytes(4))[0]
    return base64.b64encode(str(value).encode("utf-8")).decode("ascii")


def _headers(token: str, body: str) -> dict:
    return {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Content-Length": str(len(body.encode("utf-8"))),
        "X-WECHAT-UIN": _random_wechat_uin(),
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
        "Authorization": f"Bearer {token}",
    }


async def send_wechat_message(token: str, to_user_id: str, text: str):
    """Send a text message via iLink WeChat API."""
    if not token or not to_user_id or not text:
        return

    message = {
        "from_user_id": "",
        "to_user_id": to_user_id,
        "client_id": str(uuid.uuid4()),
        "message_type": MSG_TYPE_BOT,
        "message_state": MSG_STATE_FINISH,
        "item_list": [{"type": ITEM_TEXT, "text_item": {"text": text}}],
    }
    payload = {
        "msg": message,
        "base_info": {"channel_version": CHANNEL_VERSION},
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    url = f"{ILINK_BASE_URL}/{EP_SEND_MESSAGE}"

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, content=body, headers=_headers(token, body), timeout=15)
            if resp.status_code == 200:
                logger.info(f"Sent WeChat alert ({len(text)} chars)")
            else:
                logger.error(f"WeChat send failed: HTTP {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        logger.error(f"WeChat send error: {e}")
