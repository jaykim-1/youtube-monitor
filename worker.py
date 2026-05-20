"""주기적 신규 영상 체크 워커.

Windows 작업 스케줄러에서 주기적으로 실행되도록 의도된 스크립트.
실행 방법:
    python worker.py            # 한 번 실행하고 종료
    python worker.py --no-mail  # 메일 전송 없이 DB만 업데이트
"""

import argparse
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List

# 같은 디렉터리의 모듈을 import
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import app as app_module  # noqa: E402
from notifier import send_new_videos_email, NotifierError, is_email_configured  # noqa: E402
from telegram_notifier import (  # noqa: E402
    send_new_videos_telegram,
    TelegramNotifierError,
    is_telegram_configured,
)
from transcript import fetch_transcript, TranscriptError  # noqa: E402
from summarizer import summarize_video, SummarizerError  # noqa: E402


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(ROOT / "worker.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("worker")


def detect_new_videos(channel: Dict, max_results: int, longform_only: bool = True) -> List[Dict]:
    """채널의 최근 영상을 가져와, DB에 신규로 들어간 항목만 반환.
    각 영상 dict에 channel_title 과 notify_enabled 정보 추가."""
    videos = app_module.fetch_recent_videos_for_channel(
        uploads_playlist_id=channel["uploads_playlist_id"],
        max_results=max_results,
        longform_only=longform_only,
    )

    # 기존 ID 조회
    conn = app_module.get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT youtube_video_id FROM videos WHERE channel_db_id = ?",
        (channel["id"],),
    )
    existing_ids = {row[0] for row in cur.fetchall()}
    conn.close()

    new_videos = [v for v in videos if v["youtube_video_id"] not in existing_ids]

    # 모두 upsert
    app_module.upsert_videos(channel["id"], videos)

    # 롱폼만 알림 대상
    new_longform = [v for v in new_videos if not v.get("is_short")]

    # 알림 정보 보강 (채널 단위 알림 토글 포함)
    notify_enabled = bool(channel.get("notify_enabled", 1))
    for v in new_longform:
        v["channel_title"] = channel["title"]
        v["notify_enabled"] = notify_enabled

    return new_longform


def auto_summarize(video_db_id: int, youtube_video_id: str, title: str, description: str) -> bool:
    """자막 → Gemini 요약. 자막 실패 시 description으로 폴백. 성공 시 True."""
    app_module.update_video_summary_status(video_db_id, "in_progress")

    transcript_text = ""
    lang = ""
    transcript_ok = False

    try:
        transcript_text, lang = fetch_transcript(youtube_video_id)
        transcript_ok = True
    except TranscriptError as e:
        log.warning("자막 추출 실패 [%s] (description 폴백): %s", title, e)
    except Exception as e:
        log.warning("자막 추출 예외 [%s] (description 폴백): %s", title, e)

    if not transcript_ok and not (description or "").strip():
        app_module.update_video_summary_status(video_db_id, "failed")
        log.warning("자막도 description도 없음 [%s] — 요약 스킵", title)
        return False

    try:
        summary, model = summarize_video(
            title=title,
            transcript=transcript_text,
            description=description or "",
        )
    except SummarizerError as e:
        app_module.update_video_summary_status(video_db_id, "failed")
        log.error("요약 생성 실패 [%s]: %s", title, e)
        return False

    app_module.update_video_summary(
        video_db_id=video_db_id,
        summary_text=summary,
        model=model,
        transcript_text=transcript_text if transcript_ok else None,
        transcript_lang=lang if transcript_ok else None,
    )
    source = "자막" if transcript_ok else "description"
    log.info("요약 완료 [%s] (모델: %s, 출처: %s)", title, model, source)
    return True


def get_video_db_id(youtube_video_id: str) -> int:
    conn = app_module.get_connection()
    cur = conn.cursor()
    cur.execute("SELECT id FROM videos WHERE youtube_video_id = ?", (youtube_video_id,))
    row = cur.fetchone()
    conn.close()
    return int(row["id"]) if row else 0


def get_retry_candidates(limit: int = 10, max_retries: int = 3) -> List[Dict]:
    """summary_status='failed' & retry_count < max_retries 인 롱폼 영상 조회"""
    conn = app_module.get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT v.id, v.youtube_video_id, v.title, v.description,
               COALESCE(v.summary_retry_count, 0) AS summary_retry_count
        FROM videos v
        JOIN channels c ON c.id = v.channel_db_id
        WHERE v.summary_status = 'failed'
          AND v.is_short = 0
          AND c.active = 1
          AND COALESCE(v.summary_retry_count, 0) < ?
        ORDER BY v.published_at DESC
        LIMIT ?
        """,
        (max_retries, limit),
    )
    rows = [dict(row) for row in cur.fetchall()]
    conn.close()
    return rows


def increment_retry_count(video_db_id: int):
    conn = app_module.get_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE videos SET summary_retry_count = COALESCE(summary_retry_count, 0) + 1 WHERE id = ?",
        (video_db_id,),
    )
    conn.commit()
    conn.close()


def mark_notified(video_ids: List[str]):
    if not video_ids:
        return
    conn = app_module.get_connection()
    cur = conn.cursor()
    placeholders = ",".join(["?"] * len(video_ids))
    cur.execute(
        f"UPDATE videos SET notified = 1 WHERE youtube_video_id IN ({placeholders})",
        video_ids,
    )
    conn.commit()
    conn.close()


# ===== 알림 환경 (조용시간 / 다이제스트) =====

def _now_kst_hour() -> int:
    """현재 UTC를 KST(+9)로 환산해 시(0~23) 반환"""
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(hours=9)).hour


def is_within_quiet_hours() -> bool:
    if app_module.get_int_setting("quiet_hours_enabled", 0) != 1:
        return False
    qs = app_module.get_int_setting("quiet_start_kst", 23)
    qe = app_module.get_int_setting("quiet_end_kst", 7)
    h = _now_kst_hour()
    if qs == qe:
        return False
    if qs < qe:
        return qs <= h < qe
    # 자정 넘어가는 케이스 (예: 23~7)
    return h >= qs or h < qe


def is_digest_time() -> bool:
    """다이제스트 모드 + 현재 KST가 발송 시각인지"""
    if app_module.get_setting("notify_mode", "instant") != "digest":
        return False
    target = app_module.get_int_setting("digest_hour_kst", 9)
    return _now_kst_hour() == target


def get_pending_unnotified() -> List[Dict]:
    """notified=0 인 롱폼 영상 (활성·알림ON 채널)"""
    conn = app_module.get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT v.id, v.youtube_video_id, v.title, v.url, v.published_at,
               v.duration_seconds, v.is_short, v.description, v.thumbnail_url,
               v.summary_text, c.title AS channel_title
        FROM videos v
        JOIN channels c ON c.id = v.channel_db_id
        WHERE v.notified = 0
          AND v.is_short = 0
          AND c.active = 1
          AND c.notify_enabled = 1
        ORDER BY v.published_at DESC
        LIMIT 100
        """
    )
    rows = [dict(row) for row in cur.fetchall()]
    conn.close()
    for v in rows:
        v["notify_enabled"] = True
    return rows


def run_once(
    max_results: int = 30,
    send_mail: bool = True,
    longform_only: bool = True,
    auto_summarize_new: bool = True,
) -> Dict:
    app_module.init_db()

    channels = app_module.get_channels()
    if not channels:
        log.info("등록된 채널이 없습니다.")
        return {"channels": 0, "new": 0, "summarized": 0, "mail_sent": False}

    all_new: List[Dict] = []

    for channel in channels:
        try:
            new_videos = detect_new_videos(channel, max_results, longform_only)
            log.info(
                "채널 [%s]: 신규 롱폼 %d개",
                channel["title"],
                len(new_videos),
            )
            all_new.extend(new_videos)
        except Exception as e:
            log.error("채널 [%s] 처리 중 오류: %s", channel["title"], e)

    # 자동 요약
    summarized_count = 0
    if all_new and auto_summarize_new:
        log.info("자동 요약 시작: %d개 영상", len(all_new))
        for v in all_new:
            video_db_id = get_video_db_id(v["youtube_video_id"])
            if not video_db_id:
                continue
            ok = auto_summarize(
                video_db_id=video_db_id,
                youtube_video_id=v["youtube_video_id"],
                title=v["title"],
                description=v.get("description") or "",
            )
            if ok:
                summarized_count += 1
                # 메일 본문에 요약을 실어 보낼 수 있도록 메모리 객체에도 반영
                fresh = app_module.get_video(video_db_id)
                if fresh:
                    v["summary_text"] = fresh.get("summary_text")
            else:
                increment_retry_count(video_db_id)

    # 이전 실패 영상 재시도
    retried_count = 0
    if auto_summarize_new:
        retry_candidates = get_retry_candidates(limit=10, max_retries=3)
        if retry_candidates:
            log.info("실패 요약 재시도: %d개", len(retry_candidates))
            for rc in retry_candidates:
                ok = auto_summarize(
                    video_db_id=rc["id"],
                    youtube_video_id=rc["youtube_video_id"],
                    title=rc["title"],
                    description=rc.get("description") or "",
                )
                if ok:
                    retried_count += 1
                else:
                    increment_retry_count(rc["id"])

    # 채널별 알림 토글 적용 — notify_enabled=False 채널의 영상은 알림에서 제외
    notify_targets = [v for v in all_new if v.get("notify_enabled", True)]
    muted_count = len(all_new) - len(notify_targets)
    if muted_count:
        log.info("알림 차단된 영상: %d개 (채널 알림 OFF)", muted_count)

    # 알림 모드 / 조용 시간 / 다이제스트 정책 적용
    mode = app_module.get_setting("notify_mode", "instant")
    in_quiet = is_within_quiet_hours()
    digest_due = is_digest_time()

    should_dispatch = True
    dispatch_payload = notify_targets

    if in_quiet:
        log.info("조용 시간 — 알림 보류, notified=0 유지")
        should_dispatch = False
    elif mode == "digest":
        if digest_due:
            # 다이제스트 발송 시각 — notified=0 누적분 전부 발송
            pending = get_pending_unnotified()
            log.info("다이제스트 발송 시각 — 누적 %d개 발송", len(pending))
            dispatch_payload = pending
        else:
            log.info("다이제스트 모드 — 발송 시각 아님, 알림 누적")
            should_dispatch = False

    # 대기 알림 개수 settings에 기록 (UI 표시용)
    pending_count = 0
    try:
        if not should_dispatch:
            pending_count = len(get_pending_unnotified())
        app_module.set_setting("pending_notify_count", str(pending_count))
    except Exception:
        pass

    mail_sent = False
    telegram_sent = False
    if should_dispatch and dispatch_payload and send_mail:
        if is_email_configured():
            try:
                send_new_videos_email(dispatch_payload)
                mail_sent = True
                log.info("메일 발송 완료: %d개 영상", len(dispatch_payload))
            except NotifierError as e:
                log.error("메일 발송 실패: %s", e)
        else:
            log.info("SMTP 미설정 — 메일 건너뜀")

        if is_telegram_configured():
            try:
                send_new_videos_telegram(dispatch_payload)
                telegram_sent = True
                log.info("Telegram 발송 완료: %d개 영상", len(dispatch_payload))
            except TelegramNotifierError as e:
                log.error("Telegram 발송 실패: %s", e)
        else:
            log.info("Telegram 미설정 — 건너뜀")

        if mail_sent or telegram_sent:
            mark_notified([v["youtube_video_id"] for v in dispatch_payload])
            try:
                app_module.set_setting("pending_notify_count", "0")
            except Exception:
                pass

    summary = {
        "channels": len(channels),
        "new": len(all_new),
        "summarized": summarized_count,
        "retried_recovered": retried_count,
        "mail_sent": mail_sent,
        "telegram_sent": telegram_sent,
    }
    log.info("실행 완료: %s", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description="YouTube Monitor 신규 영상 체크 워커")
    parser.add_argument("--max-results", type=int, default=30)
    parser.add_argument("--no-mail", action="store_true", help="메일 전송 건너뛰기")
    parser.add_argument("--include-shorts", action="store_true", help="숏폼도 저장/알림 (기본은 롱폼만)")
    parser.add_argument("--no-summary", action="store_true", help="자동 요약 건너뛰기")
    args = parser.parse_args()

    try:
        run_once(
            max_results=args.max_results,
            send_mail=not args.no_mail,
            longform_only=not args.include_shorts,
            auto_summarize_new=not args.no_summary,
        )
    except Exception as e:
        log.exception("워커 실행 중 치명적 오류: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
