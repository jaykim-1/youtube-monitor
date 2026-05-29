"""Telegram Bot 알림 모듈"""

import html
import logging
import os
import time
from typing import List, Dict

import requests
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_API_BASE = "https://api.telegram.org"

MAX_MESSAGE_LEN = 4000  # Telegram 한계 4096, 여유 두기
INTER_MESSAGE_DELAY_SEC = 0.5  # 같은 채팅 1초당 1메시지 권장 — 안전 마진

log = logging.getLogger(__name__)


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

    # 영상마다 별도 메시지 (요약 길이 때문에 묶기 어려움).
    # 동일 chat에 빠른 연속 발송은 429를 유발하므로 사이에 짧은 sleep.
    for i, v in enumerate(new_videos):
        text = _format_video_message(v)
        _send_message(text)
        if i < len(new_videos) - 1:
            time.sleep(INTER_MESSAGE_DELAY_SEC)


def _format_video_message(v: Dict) -> str:
    """HTML 모드용 메시지 포맷. 본문 모든 텍스트를 escape."""
    title = v.get("title", "")
    channel = v.get("channel_title", "?")
    url = v.get("url", "")
    published = (v.get("published_at") or "")[:16].replace("T", " ")
    summary = (v.get("summary_text") or "").strip()

    parts = [
        f"🎥 <b>{_escape_html(channel)}</b>",
        f"<i>{_escape_html(published)}</i>",
        "",
        f"<b>{_escape_html(title)}</b>",
        _escape_html(url),
    ]
    if summary:
        summary_short = summary[:MAX_MESSAGE_LEN - 500]
        parts.extend(["", "📝 요약:", _escape_html(summary_short)])

    return "\n".join(parts)


def _escape_html(text: str) -> str:
    """Telegram HTML 모드에서 의미를 가지는 < > & 를 escape. None-safe."""
    return html.escape(text or "", quote=False)


def _send_message(text: str) -> None:
    url = f"{TELEGRAM_API_BASE}/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }

    # 1차 발송
    try:
        resp = requests.post(url, json=payload, timeout=15)
    except Exception as e:
        raise TelegramNotifierError(f"Telegram 요청 실패: {e}")

    # 레이트 리밋(429) — Telegram이 알려주는 retry_after만큼 대기 후 재시도
    if resp.status_code == 429:
        try:
            retry_after = int(resp.json().get("parameters", {}).get("retry_after", 3))
        except Exception:
            retry_after = 3
        log.warning("Telegram rate-limited, sleeping %ds", retry_after + 1)
        time.sleep(retry_after + 1)
        try:
            resp = requests.post(url, json=payload, timeout=15)
        except Exception as e:
            raise TelegramNotifierError(f"Telegram 재시도(429) 실패: {e}")

    # 그 외 실패는 parse_mode를 제거(Plain text)하고 한 번 더 시도
    if resp.status_code != 200:
        payload.pop("parse_mode", None)
        try:
            resp2 = requests.post(url, json=payload, timeout=15)
        except Exception as e:
            raise TelegramNotifierError(f"Telegram 재시도 실패: {e}")
        if resp2.status_code != 200:
            raise TelegramNotifierError(
                f"Telegram 발송 실패: {resp.status_code} {resp.text[:300]}"
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
