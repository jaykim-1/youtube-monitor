"""Telegram CHAT_ID 자동 탐지 도구.

사용:
    1) .env에 TELEGRAM_BOT_TOKEN 입력
    2) 본인의 텔레그램에서 봇 검색 → /start 또는 아무 메시지 한 번 전송
    3) 이 스크립트 실행:  python tools/find_telegram_chat_id.py
    4) 출력된 CHAT_ID를 .env의 TELEGRAM_CHAT_ID에 넣기
    5) 검증:  python tools/find_telegram_chat_id.py --test
"""

import argparse
import sys
from pathlib import Path

# Windows cp949 콘솔에서도 깨지지 않도록 UTF-8 강제
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telegram_notifier import (
    discover_chat_id,
    send_test_message,
    TelegramNotifierError,
    is_telegram_configured,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", help="설정된 토큰/CHAT_ID로 테스트 메시지 발송")
    args = parser.parse_args()

    if args.test:
        if not is_telegram_configured():
            print("[X] .env에 TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID 미설정")
            sys.exit(1)
        try:
            send_test_message()
            print("[OK] 테스트 메시지 발송 성공 — 텔레그램 확인하세요")
        except TelegramNotifierError as e:
            print(f"[X] 실패: {e}")
            sys.exit(1)
        return

    try:
        chat_id = discover_chat_id()
        print(f"[OK] CHAT_ID = {chat_id}")
        print(".env 파일에 다음 줄을 추가/수정하세요:")
        print(f"TELEGRAM_CHAT_ID={chat_id}")
    except TelegramNotifierError as e:
        print(f"[X] {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
