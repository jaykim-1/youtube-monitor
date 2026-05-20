"""유튜브 자막 추출 모듈

1차: youtube-transcript-api (빠르지만 클라우드 IP에서 자주 차단됨)
2차: yt-dlp (브라우저 흉내, 클라우드에서도 자주 성공)
"""

import json
import logging
import tempfile
from pathlib import Path
from typing import List, Tuple, Optional

try:
    from youtube_transcript_api import (
        YouTubeTranscriptApi,
        NoTranscriptFound,
        TranscriptsDisabled,
        VideoUnavailable,
    )
except ImportError as e:
    raise ImportError(
        "youtube-transcript-api 패키지가 필요합니다. `pip install youtube-transcript-api`"
    ) from e

log = logging.getLogger(__name__)

PREFERRED_LANGS: List[str] = ["ko", "en", "ja", "zh-Hans", "zh-Hant"]


class TranscriptError(Exception):
    pass


def fetch_transcript(video_id: str) -> Tuple[str, str]:
    """주어진 video_id의 자막을 가져와 (text, lang) 반환.
    youtube-transcript-api 실패 시 yt-dlp로 한 번 더 시도.
    """
    try:
        return _fetch_via_transcript_api(video_id)
    except TranscriptError as e:
        log.info("youtube-transcript-api 실패 (%s), yt-dlp 폴백 시도", e)
    except Exception as e:
        log.info("youtube-transcript-api 예외 (%s), yt-dlp 폴백 시도", e)

    return _fetch_via_yt_dlp(video_id)


def _fetch_via_transcript_api(video_id: str) -> Tuple[str, str]:
    api = YouTubeTranscriptApi()

    # 1) list() → 수동/자동 분리 시도
    try:
        transcript_list = api.list(video_id)
    except TranscriptsDisabled:
        raise TranscriptError("이 영상은 자막이 비활성화되어 있습니다.")
    except VideoUnavailable:
        raise TranscriptError("영상을 찾을 수 없습니다.")
    except Exception:
        # list() 실패 시 fetch()로 곧장 폴백
        return _fallback_fetch(api, video_id)

    # 수동 자막 우선
    for lang in PREFERRED_LANGS:
        try:
            t = transcript_list.find_manually_created_transcript([lang])
            return _join(t.fetch()), lang
        except Exception:
            continue

    # 자동 생성 자막
    for lang in PREFERRED_LANGS:
        try:
            t = transcript_list.find_generated_transcript([lang])
            return _join(t.fetch()), lang
        except Exception:
            continue

    # 아무 언어나
    try:
        for t in transcript_list:
            return _join(t.fetch()), getattr(t, "language_code", "unknown")
    except Exception:
        pass

    return _fallback_fetch(api, video_id)


def _fallback_fetch(api: "YouTubeTranscriptApi", video_id: str) -> Tuple[str, str]:
    try:
        fetched = api.fetch(video_id, languages=PREFERRED_LANGS)
    except TranscriptsDisabled:
        raise TranscriptError("자막이 비활성화된 영상입니다.")
    except NoTranscriptFound:
        raise TranscriptError("사용 가능한 자막을 찾지 못했습니다.")
    except VideoUnavailable:
        raise TranscriptError("영상을 찾을 수 없습니다.")
    except Exception as e:
        raise TranscriptError(f"자막 가져오기 실패: {e}")

    return _join(fetched), getattr(fetched, "language_code", "unknown")


def _join(segments) -> str:
    """list of snippets or FetchedTranscript → 단일 텍스트"""
    parts = []

    # FetchedTranscript에는 .snippets 속성이 있음 (v1.0+)
    items = getattr(segments, "snippets", None) or segments

    for seg in items:
        text = ""
        if isinstance(seg, dict):
            text = seg.get("text", "").strip()
        else:
            text = getattr(seg, "text", "").strip()
        if text:
            parts.append(text)

    return " ".join(parts)


# ===== yt-dlp 폴백 =====

def _fetch_via_yt_dlp(video_id: str) -> Tuple[str, str]:
    """yt-dlp로 자막 받기 — 브라우저 흉내라 클라우드에서도 성공률 높음.
    수동 자막 우선, 없으면 자동 생성. JSON3 포맷으로 받아 파싱.
    """
    try:
        from yt_dlp import YoutubeDL
    except ImportError as e:
        raise TranscriptError("yt-dlp 패키지가 필요합니다.") from e

    url = f"https://www.youtube.com/watch?v={video_id}"

    with tempfile.TemporaryDirectory() as tmp:
        outtmpl = str(Path(tmp) / "%(id)s.%(ext)s")
        last_err: Optional[Exception] = None

        # 시도 순서: 수동 자막 → 자동 생성 자막
        for use_auto in (False, True):
            opts = {
                "skip_download": True,
                "writesubtitles": not use_auto,
                "writeautomaticsub": use_auto,
                "subtitleslangs": PREFERRED_LANGS,
                "subtitlesformat": "json3",
                "outtmpl": outtmpl,
                "quiet": True,
                "no_warnings": True,
                "ignoreerrors": False,
            }
            try:
                with YoutubeDL(opts) as ydl:
                    ydl.download([url])
            except Exception as e:
                last_err = e
                continue

            # 다운로드된 자막 파일 찾기
            for lang in PREFERRED_LANGS:
                for ext in ("json3", "json"):
                    candidate = Path(tmp) / f"{video_id}.{lang}.{ext}"
                    if candidate.exists():
                        text = _parse_json3(candidate.read_text(encoding="utf-8"))
                        if text:
                            return text, lang
            # 임의 언어
            for p in Path(tmp).iterdir():
                if p.suffix in (".json3", ".json"):
                    text = _parse_json3(p.read_text(encoding="utf-8"))
                    if text:
                        lang = p.stem.split(".")[-1] or "unknown"
                        return text, lang

        raise TranscriptError(
            f"yt-dlp로도 자막을 찾지 못했습니다: {last_err if last_err else '없음'}"
        )


def _parse_json3(text: str) -> str:
    """YouTube json3 자막 → 단일 텍스트"""
    try:
        data = json.loads(text)
    except Exception:
        return ""
    parts = []
    for event in data.get("events", []) or []:
        for seg in event.get("segs", []) or []:
            t = (seg.get("utf8") or "").strip()
            if t:
                parts.append(t)
    return " ".join(parts).strip()
