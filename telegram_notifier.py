"""Telegram Bot 알림 모듈"""

import os
from typing import List, Dict

import requests
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_API_BASE = "https://api.telegram.org"

MAX_MESSAGE_LEN = 4000  # Telegram 한계 4096, 여유 두기


class TelegramNotifierError(Exception):
    pass


def is_telegram_configured() -> bool:
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


def send_new_videos_telegram(new_videos: List[Dict]) -> None:
    if not new_videos:
        return

    if not is_telegram_configured():
        raise TelegramNotifierError(
            "Telegram 설정이 부족합니다. .env에 TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID를 설정하세요."
        )

    # 영상마다 별도 메시지 (요약 길이 때문에 묶기 어려움)
    for v in new_videos:
        text = _format_video_message(v)
        _send_message(text)


def _format_video_message(v: Dict) -> str:
    title = v.get("title", "")
    channel = v.get("channel_title", "?")
    url = v.get("url", "")
    published = (v.get("published_at") or "")[:16].replace("T", " ")
    summary = (v.get("summary_text") or "").strip()

    parts = [
        f"🎥 *{_escape_md(channel)}*",
        f"_{_escape_md(published)}_",
        "",
        f"*{_escape_md(title)}*",
        url,
    ]
    if summary:
        # 텔레그램 마크다운 v2는 까다로워서 그냥 plain으로 요약 보내고 헤더만 굵게
        summary_short = summary[:MAX_MESSAGE_LEN - 500]
        parts.extend(["", "📝 요약:", summary_short])

    return "\n".join(parts)


def _escape_md(text: str) -> str:
    """Telegram Markdown(v1)에서 충돌하는 최소 문자만 escape"""
    for c in ["_", "*", "[", "]", "`"]:
        text = text.replace(c, "\\" + c)
    return text


def _send_message(text: str) -> None:
    url = f"{TELEGRAM_API_BASE}/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False,
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
    except Exception as e:
        raise TelegramNotifierError(f"Telegram 요청 실패: {e}")

    if resp.status_code != 200:
        # parse_mode 문제로 실패하는 경우 plain text 재시도
        payload["parse_mode"] = ""
        try:
            resp2 = requests.post(url, json=payload, timeout=15)
        except Exception as e:
            raise TelegramNotifierError(f"Telegram 재시도 실패: {e}")
        if resp2.status_code != 200:
            raise TelegramNotifierError(
                f"Telegram 발송 실패: {resp.status_code} {resp.text}"
            )


def send_test_message() -> str:
    """봇/Chat ID 검증용 테스트 메시지 발송"""
    if not is_telegram_configured():
        raise TelegramNotifierError("TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID 미설정")
    _send_message("✅ YouTube Monitor 연결 테스트 성공")
    return "ok"


def discover_chat_id() -> str:
    """사용자가 본인 봇에게 메시지를 보낸 후 호출하면 chat_id를 찾아 반환"""
    if not TELEGRAM_BOT_TOKEN:
        raise TelegramNotifierError("TELEGRAM_BOT_TOKEN 미설정")
    url = f"{TELEGRAM_API_BASE}/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    resp = requests.get(url, timeout=15)
    if resp.status_code != 200:
        raise TelegramNotifierError(f"getUpdates 실패: {resp.status_code} {resp.text}")
    data = resp.json()
    for upd in reversed(data.get("result", [])):
        msg = upd.get("message") or upd.get("channel_post") or {}
        chat = msg.get("chat") or {}
        cid = chat.get("id")
        if cid:
            return str(cid)
    raise TelegramNotifierError(
        "최근 메시지가 없습니다. 본인 텔레그램에서 봇 검색 → /start 또는 아무 메시지 한 번 보낸 후 다시 시도하세요."
    )
