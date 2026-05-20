"""Google Gemini 기반 요약 모듈 (google-genai SDK)"""

import os
import time
from typing import List, Tuple, Optional

from dotenv import load_dotenv

load_dotenv()


def _read_secret(name: str, default: str = "") -> str:
    """환경변수 → st.secrets 순서로 시도. Streamlit 미설치/미초기화 시 무시."""
    val = os.getenv(name, "")
    if val:
        return val
    try:
        import streamlit as st  # type: ignore
        return st.secrets.get(name, default) or default
    except Exception:
        return default


GEMINI_API_KEY = _read_secret("GOOGLE_API_KEY") or _read_secret("GEMINI_API_KEY")
GEMINI_MODEL = _read_secret("GEMINI_MODEL", "gemini-3.5-flash")


class SummarizerError(Exception):
    pass


def _get_client():
    if not GEMINI_API_KEY:
        raise SummarizerError(
            "GOOGLE_API_KEY가 설정되어 있지 않습니다. .env에 키를 추가하세요."
        )
    try:
        from google import genai
    except ImportError as e:
        raise SummarizerError(
            "google-genai 패키지가 필요합니다. `pip install google-genai`"
        ) from e

    return genai.Client(api_key=GEMINI_API_KEY)


VIDEO_SUMMARY_PROMPT = """다음은 유튜브 영상의 자막입니다. 한국어로 다음 구조에 맞춰 요약해주세요.

**제목:** {title}

**자막:**
{transcript}

---

요약 형식:
1. **한 줄 요약** (1문장)
2. **핵심 포인트** (3~5개 불릿)
3. **주요 인사이트 / 시사점** (2~3문장)
4. **언급된 주요 인물·서비스·키워드** (있을 경우)

중요:
- 자막에 없는 내용은 절대 만들지 마세요.
- 광고/협찬 멘트는 제외하세요.
- 가독성 있게 마크다운으로 작성하세요.
"""


TREND_SUMMARY_PROMPT = """다음은 특정 기간 동안 여러 유튜브 채널에 업로드된 롱폼 영상 목록입니다.
업계 트렌드 관점에서 한국어로 분석/요약해주세요.

영상 목록:
{video_list}

---

분석 형식:
1. **이 기간의 핵심 트렌드** (3~5개)
2. **반복되는 주제 / 키워드**
3. **채널별 포지셔닝 차이** (눈에 띄는 경우)
4. **다음 주에 주목할 만한 흐름** (예측)

중요:
- 제공된 데이터에 기반해서만 분석하세요. 추측은 명시적으로 표시.
- 마크다운으로 작성하세요.
"""


MAX_TRANSCRIPT_CHARS = 60000
TREND_MAX_VIDEOS = 60  # 트렌드 분석에 쓸 최신 영상 수 상한

# 503/429 발생 시 fallback 시도할 모델 순서
FALLBACK_MODELS = [
    GEMINI_MODEL,
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
    "gemini-1.5-flash",
]


def _call_gemini_with_retry(client, prompt: str, max_attempts: int = 3) -> Tuple[str, str]:
    """Gemini 호출 + 503/429 시 백오프 재시도 + 모델 fallback.
    (응답 텍스트, 실제 사용된 모델명) 반환.
    """
    last_err = None
    tried_models = []

    for model_name in FALLBACK_MODELS:
        if model_name in tried_models or not model_name:
            continue
        tried_models.append(model_name)

        for attempt in range(max_attempts):
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                )
                text = _extract_text(response)
                if text:
                    return text.strip(), model_name
                last_err = SummarizerError("Gemini 응답이 비어 있습니다.")
                break  # 빈 응답은 재시도 의미 없음, 다음 모델로
            except Exception as e:
                msg = str(e)
                last_err = e
                # 일시적 오류만 재시도
                if any(code in msg for code in ["503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED"]):
                    if attempt < max_attempts - 1:
                        time.sleep(2 ** attempt)  # 1s, 2s
                        continue
                # 그 외 에러는 다음 모델로
                break

    raise SummarizerError(
        f"Gemini 호출 실패 (모델 {len(tried_models)}개 시도): {last_err}"
    )


def summarize_video(
    title: str,
    transcript: str,
    description: Optional[str] = None,
) -> Tuple[str, str]:
    """(요약 텍스트, 사용 모델명) 반환"""
    client = _get_client()

    if not transcript or not transcript.strip():
        if description:
            transcript = f"[자막 없음. 설명란을 기반으로 요약]\n{description}"
        else:
            raise SummarizerError("요약할 자막/설명이 없습니다.")

    if len(transcript) > MAX_TRANSCRIPT_CHARS:
        transcript = transcript[:MAX_TRANSCRIPT_CHARS] + "\n...(이하 생략)"

    prompt = VIDEO_SUMMARY_PROMPT.format(
        title=title or "(제목 없음)",
        transcript=transcript,
    )

    return _call_gemini_with_retry(client, prompt)


def summarize_trend(videos: List[dict]) -> Tuple[str, str]:
    client = _get_client()

    if not videos:
        raise SummarizerError("분석할 영상이 없습니다.")

    # 최신 영상부터 N개로 제한 (이미 published_at DESC 정렬 가정)
    videos = videos[:TREND_MAX_VIDEOS]

    lines = []
    for v in videos:
        title = (v.get("title") or "").strip()
        channel = (v.get("channel_title") or "").strip()
        published = (v.get("published_at") or "")[:10]
        duration_min = int((v.get("duration_seconds") or 0) / 60)
        summary = (v.get("summary_text") or "").strip()
        excerpt = summary[:200] + ("..." if len(summary) > 200 else "") if summary else ""
        lines.append(
            f"- [{channel}] ({published}, {duration_min}분) {title}"
            + (f"\n  요약: {excerpt}" if excerpt else "")
        )

    prompt = TREND_SUMMARY_PROMPT.format(video_list="\n".join(lines))

    return _call_gemini_with_retry(client, prompt)


def _extract_text(response) -> str:
    try:
        if response.text:
            return response.text
    except Exception:
        pass
    try:
        for c in (response.candidates or []):
            for p in (getattr(c.content, "parts", []) or []):
                t = getattr(p, "text", "")
                if t:
                    return t
    except Exception:
        pass
    return ""
