"""유튜브 자막 추출 모듈

`youtube-transcript-api` 라이브러리를 사용하여 자막을 가져온다.
이 API는 비공식이며 YouTube의 변경에 영향을 받을 수 있다.
"""

from typing import List, Tuple

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


PREFERRED_LANGS: List[str] = ["ko", "en", "ja", "zh-Hans", "zh-Hant"]


class TranscriptError(Exception):
    pass


def fetch_transcript(video_id: str) -> Tuple[str, str]:
    """주어진 video_id의 자막을 가져와 (text, lang) 반환.

    선호 언어 순서대로 시도하고, 없으면 사용 가능한 자동 생성 자막을 사용한다.
    """
    try:
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
    except TranscriptsDisabled:
        raise TranscriptError("이 영상은 자막이 비활성화되어 있습니다.")
    except VideoUnavailable:
        raise TranscriptError("영상을 찾을 수 없습니다.")
    except Exception as e:
        raise TranscriptError(f"자막 목록을 가져오는 중 오류: {e}")

    # 1) 수동 자막 우선
    for lang in PREFERRED_LANGS:
        try:
            t = transcript_list.find_manually_created_transcript([lang])
            return _join_transcript(t.fetch()), lang
        except Exception:
            continue

    # 2) 자동 생성 자막
    for lang in PREFERRED_LANGS:
        try:
            t = transcript_list.find_generated_transcript([lang])
            return _join_transcript(t.fetch()), lang
        except Exception:
            continue

    # 3) 아무 언어나
    try:
        for t in transcript_list:
            return _join_transcript(t.fetch()), t.language_code
    except Exception:
        pass

    raise TranscriptError("사용 가능한 자막을 찾지 못했습니다.")


def _join_transcript(segments: List[dict]) -> str:
    parts = []
    for seg in segments:
        text = seg.get("text", "").strip()
        if text:
            parts.append(text)
    return " ".join(parts)
