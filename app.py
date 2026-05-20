import os
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests
import streamlit as st
import isodate
from dotenv import load_dotenv

from summarizer import summarize_video, SummarizerError
from transcript import fetch_transcript, TranscriptError


# =========================================================
# 기본 설정
# =========================================================

load_dotenv()

YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
YOUTUBE_API_BASE_URL = "https://www.googleapis.com/youtube/v3"
DB_PATH = Path(__file__).parent / "youtube_monitor.db"


# =========================================================
# 예외 클래스
# =========================================================

class YouTubeAPIError(Exception):
    pass


# =========================================================
# DB 관련 함수
# =========================================================

def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            youtube_channel_id TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            url TEXT,
            thumbnail_url TEXT,
            uploads_playlist_id TEXT NOT NULL,
            active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            youtube_video_id TEXT UNIQUE NOT NULL,
            channel_db_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            url TEXT NOT NULL,
            published_at TEXT,
            duration_seconds INTEGER,
            is_short INTEGER DEFAULT 0,
            description TEXT,
            thumbnail_url TEXT,
            summary_status TEXT DEFAULT 'not_started',
            summary_text TEXT,
            summary_model TEXT,
            summary_updated_at TIMESTAMP,
            transcript_text TEXT,
            transcript_lang TEXT,
            notified INTEGER DEFAULT 0,
            seen INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(channel_db_id) REFERENCES channels(id)
        )
        """
    )

    # 누락 컬럼 보강 (기존 DB 호환)
    _ensure_column(cur, "videos", "summary_text", "TEXT")
    _ensure_column(cur, "videos", "summary_model", "TEXT")
    _ensure_column(cur, "videos", "summary_updated_at", "TIMESTAMP")
    _ensure_column(cur, "videos", "transcript_text", "TEXT")
    _ensure_column(cur, "videos", "transcript_lang", "TEXT")
    _ensure_column(cur, "videos", "notified", "INTEGER DEFAULT 0")
    _ensure_column(cur, "videos", "seen", "INTEGER DEFAULT 0")

    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_videos_channel_short_published
        ON videos(channel_db_id, is_short, published_at DESC)
        """
    )

    conn.commit()
    conn.close()


def _ensure_column(cur, table: str, column: str, definition: str):
    cur.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in cur.fetchall()}
    if column not in existing:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def upsert_channel(channel: Dict) -> int:
    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO channels (
            youtube_channel_id,
            title,
            url,
            thumbnail_url,
            uploads_playlist_id,
            active
        )
        VALUES (?, ?, ?, ?, ?, 1)
        ON CONFLICT(youtube_channel_id) DO UPDATE SET
            title = excluded.title,
            url = excluded.url,
            thumbnail_url = excluded.thumbnail_url,
            uploads_playlist_id = excluded.uploads_playlist_id,
            active = 1
        """,
        (
            channel["youtube_channel_id"],
            channel["title"],
            channel["url"],
            channel.get("thumbnail_url"),
            channel["uploads_playlist_id"],
        ),
    )

    conn.commit()

    cur.execute(
        """
        SELECT id
        FROM channels
        WHERE youtube_channel_id = ?
        """,
        (channel["youtube_channel_id"],),
    )

    row = cur.fetchone()
    conn.close()

    return int(row["id"])


@st.cache_data(ttl=30)
def get_channels() -> List[Dict]:
    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT *
        FROM channels
        WHERE active = 1
        ORDER BY created_at DESC
        """
    )

    rows = [dict(row) for row in cur.fetchall()]
    conn.close()

    return rows


@st.cache_data(ttl=30)
def get_inactive_channels() -> List[Dict]:
    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT *
        FROM channels
        WHERE active = 0
        ORDER BY created_at DESC
        """
    )

    rows = [dict(row) for row in cur.fetchall()]
    conn.close()

    return rows


def reactivate_channel(channel_db_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE channels SET active = 1 WHERE id = ?", (channel_db_id,))
    conn.commit()
    conn.close()


def upsert_videos(channel_db_id: int, videos: List[Dict]) -> Dict[str, int]:
    """저장 결과를 {'new': n, 'updated': m} 형태로 반환"""
    conn = get_connection()
    cur = conn.cursor()

    new_count = 0
    updated_count = 0

    for video in videos:
        cur.execute(
            "SELECT id FROM videos WHERE youtube_video_id = ?",
            (video["youtube_video_id"],),
        )
        existing = cur.fetchone()

        if existing:
            cur.execute(
                """
                UPDATE videos SET
                    title = ?,
                    url = ?,
                    published_at = ?,
                    duration_seconds = ?,
                    is_short = ?,
                    description = ?,
                    thumbnail_url = ?
                WHERE youtube_video_id = ?
                """,
                (
                    video["title"],
                    video["url"],
                    video.get("published_at"),
                    video.get("duration_seconds"),
                    1 if video.get("is_short") else 0,
                    video.get("description"),
                    video.get("thumbnail_url"),
                    video["youtube_video_id"],
                ),
            )
            updated_count += 1
        else:
            cur.execute(
                """
                INSERT INTO videos (
                    youtube_video_id,
                    channel_db_id,
                    title,
                    url,
                    published_at,
                    duration_seconds,
                    is_short,
                    description,
                    thumbnail_url,
                    summary_status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'not_started')
                """,
                (
                    video["youtube_video_id"],
                    channel_db_id,
                    video["title"],
                    video["url"],
                    video.get("published_at"),
                    video.get("duration_seconds"),
                    1 if video.get("is_short") else 0,
                    video.get("description"),
                    video.get("thumbnail_url"),
                ),
            )
            new_count += 1

    conn.commit()
    conn.close()

    return {"new": new_count, "updated": updated_count}


@st.cache_data(ttl=30)
def get_videos_by_channel(channel_db_id: int, include_shorts: bool = False) -> List[Dict]:
    conn = get_connection()
    cur = conn.cursor()

    if include_shorts:
        cur.execute(
            """
            SELECT
                id,
                youtube_video_id,
                channel_db_id,
                title,
                url,
                published_at,
                duration_seconds,
                is_short,
                thumbnail_url,
                summary_status,
                seen
            FROM videos
            WHERE channel_db_id = ?
            ORDER BY published_at DESC
            """,
            (channel_db_id,),
        )
    else:
        cur.execute(
            """
            SELECT
                id,
                youtube_video_id,
                channel_db_id,
                title,
                url,
                published_at,
                duration_seconds,
                is_short,
                thumbnail_url,
                summary_status,
                seen
            FROM videos
            WHERE channel_db_id = ?
              AND is_short = 0
            ORDER BY published_at DESC
            """,
            (channel_db_id,),
        )

    rows = [dict(row) for row in cur.fetchall()]
    conn.close()

    return rows


@st.cache_data(ttl=30)
def get_channel_video_stats(include_shorts: bool = False) -> Dict[int, Dict[str, int]]:
    """채널 선택 UI에 필요한 영상 수/NEW 수만 가볍게 조회."""
    conn = get_connection()
    cur = conn.cursor()

    if include_shorts:
        join_condition = "v.channel_db_id = c.id"
    else:
        join_condition = "v.channel_db_id = c.id AND v.is_short = 0"

    cur.execute(
        f"""
        SELECT
            c.id AS channel_id,
            COUNT(v.id) AS video_count,
            COALESCE(SUM(CASE WHEN v.seen = 0 AND v.id IS NOT NULL THEN 1 ELSE 0 END), 0) AS new_count
        FROM channels c
        LEFT JOIN videos v ON {join_condition}
        WHERE c.active = 1
        GROUP BY c.id
        """,
    )

    rows = {
        int(row["channel_id"]): {
            "video_count": int(row["video_count"] or 0),
            "new_count": int(row["new_count"] or 0),
        }
        for row in cur.fetchall()
    }
    conn.close()
    return rows


def get_video(video_db_id: int) -> Optional[Dict]:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM videos WHERE id = ?", (video_db_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def update_video_summary(
    video_db_id: int,
    summary_text: str,
    model: str,
    transcript_text: Optional[str],
    transcript_lang: Optional[str],
):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE videos SET
            summary_text = ?,
            summary_model = ?,
            summary_status = 'done',
            summary_updated_at = CURRENT_TIMESTAMP,
            transcript_text = COALESCE(?, transcript_text),
            transcript_lang = COALESCE(?, transcript_lang)
        WHERE id = ?
        """,
        (summary_text, model, transcript_text, transcript_lang, video_db_id),
    )
    conn.commit()
    conn.close()


def update_video_summary_status(video_db_id: int, status: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE videos SET summary_status = ? WHERE id = ?",
        (status, video_db_id),
    )
    conn.commit()
    conn.close()


def mark_video_seen(video_db_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE videos SET seen = 1 WHERE id = ?", (video_db_id,))
    conn.commit()
    conn.close()


@st.cache_data(ttl=30)
def get_unseen_videos(limit: int = 50) -> List[Dict]:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT v.*, c.title AS channel_title
        FROM videos v
        JOIN channels c ON c.id = v.channel_db_id
        WHERE v.seen = 0
          AND v.is_short = 0
          AND c.active = 1
        ORDER BY v.published_at DESC
        LIMIT ?
        """,
        (limit,),
    )
    rows = [dict(row) for row in cur.fetchall()]
    conn.close()
    return rows


@st.cache_data(ttl=60)
def get_videos_in_period(start_iso: str, end_iso: str, include_shorts: bool = False) -> List[Dict]:
    conn = get_connection()
    cur = conn.cursor()
    if include_shorts:
        cur.execute(
            """
            SELECT v.*, c.title AS channel_title
            FROM videos v
            JOIN channels c ON c.id = v.channel_db_id
            WHERE c.active = 1
              AND v.published_at >= ?
              AND v.published_at < ?
            ORDER BY v.published_at DESC
            """,
            (start_iso, end_iso),
        )
    else:
        cur.execute(
            """
            SELECT v.*, c.title AS channel_title
            FROM videos v
            JOIN channels c ON c.id = v.channel_db_id
            WHERE c.active = 1
              AND v.is_short = 0
              AND v.published_at >= ?
              AND v.published_at < ?
            ORDER BY v.published_at DESC
            """,
            (start_iso, end_iso),
        )
    rows = [dict(row) for row in cur.fetchall()]
    conn.close()
    return rows


def delete_channel(channel_db_id: int):
    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        UPDATE channels
        SET active = 0
        WHERE id = ?
        """,
        (channel_db_id,),
    )

    conn.commit()
    conn.close()


# =========================================================
# YouTube API 관련 함수
# =========================================================

def ensure_api_key():
    if not YOUTUBE_API_KEY:
        raise YouTubeAPIError(
            "YOUTUBE_API_KEY가 설정되어 있지 않습니다. .env 파일에 API Key를 입력하세요."
        )


def youtube_get(endpoint: str, params: Dict) -> Dict:
    ensure_api_key()

    params = {
        **params,
        "key": YOUTUBE_API_KEY,
    }

    response = requests.get(
        f"{YOUTUBE_API_BASE_URL}/{endpoint}",
        params=params,
        timeout=20,
    )

    if response.status_code != 200:
        raise YouTubeAPIError(
            f"YouTube API 요청 실패: {response.status_code}\n{response.text}"
        )

    return response.json()


def parse_youtube_channel_input(user_input: str) -> Dict[str, Optional[str]]:
    value = user_input.strip()

    if not value:
        raise ValueError("채널 URL 또는 채널 ID를 입력하세요.")

    if re.match(r"^UC[a-zA-Z0-9_-]{20,}$", value):
        return {"type": "channel_id", "value": value}

    if value.startswith("@"):
        return {"type": "handle", "value": value[1:]}

    if value.startswith("http://") or value.startswith("https://"):
        parsed = urlparse(value)
        path = parsed.path.strip("/")

        match = re.search(r"channel/(UC[a-zA-Z0-9_-]+)", path)
        if match:
            return {"type": "channel_id", "value": match.group(1)}

        match = re.search(r"@([^/]+)", path)
        if match:
            return {"type": "handle", "value": match.group(1)}

        parts = path.split("/")
        if len(parts) >= 2 and parts[0] in ["c", "user"]:
            return {"type": "search", "value": parts[1]}

        if path:
            return {"type": "search", "value": path.split("/")[-1]}

    return {"type": "search", "value": value}


def normalize_channel(item: Dict) -> Dict:
    snippet = item.get("snippet", {})
    content_details = item.get("contentDetails", {})
    related_playlists = content_details.get("relatedPlaylists", {})

    channel_id = item["id"]
    uploads_playlist_id = related_playlists.get("uploads")

    if not uploads_playlist_id:
        raise YouTubeAPIError("이 채널의 업로드 플레이리스트 ID를 찾지 못했습니다.")

    thumbnails = snippet.get("thumbnails", {})
    thumbnail_url = None

    for key in ["high", "medium", "default"]:
        if key in thumbnails:
            thumbnail_url = thumbnails[key].get("url")
            break

    return {
        "youtube_channel_id": channel_id,
        "title": snippet.get("title", "Untitled Channel"),
        "url": f"https://www.youtube.com/channel/{channel_id}",
        "thumbnail_url": thumbnail_url,
        "uploads_playlist_id": uploads_playlist_id,
    }


def get_channel_by_id(channel_id: str) -> Dict:
    data = youtube_get(
        "channels",
        {
            "part": "snippet,contentDetails",
            "id": channel_id,
            "maxResults": 1,
        },
    )

    items = data.get("items", [])

    if not items:
        raise YouTubeAPIError("해당 channelId로 채널을 찾지 못했습니다.")

    return normalize_channel(items[0])


def get_channel_by_handle(handle: str) -> Dict:
    try:
        data = youtube_get(
            "channels",
            {
                "part": "snippet,contentDetails",
                "forHandle": handle,
                "maxResults": 1,
            },
        )

        items = data.get("items", [])

        if items:
            return normalize_channel(items[0])

    except YouTubeAPIError:
        pass

    return search_channel(handle)


def search_channel(query: str) -> Dict:
    data = youtube_get(
        "search",
        {
            "part": "snippet",
            "q": query,
            "type": "channel",
            "maxResults": 1,
        },
    )

    items = data.get("items", [])

    if not items:
        raise YouTubeAPIError("검색 결과에서 채널을 찾지 못했습니다.")

    channel_id = items[0]["snippet"]["channelId"]

    return get_channel_by_id(channel_id)


def resolve_channel(user_input: str) -> Dict:
    parsed = parse_youtube_channel_input(user_input)

    if parsed["type"] == "channel_id":
        return get_channel_by_id(parsed["value"])

    if parsed["type"] == "handle":
        return get_channel_by_handle(parsed["value"])

    return search_channel(parsed["value"])


def get_recent_video_ids_from_uploads_playlist(
    uploads_playlist_id: str,
    max_results: int = 20,
    page_token: Optional[str] = None,
) -> Tuple[List[str], Optional[str]]:
    """(video_ids, next_page_token) 반환. next_page_token이 None이면 끝."""
    params = {
        "part": "contentDetails",
        "playlistId": uploads_playlist_id,
        "maxResults": min(max_results, 50),
    }
    if page_token:
        params["pageToken"] = page_token

    data = youtube_get("playlistItems", params)

    video_ids = []
    for item in data.get("items", []):
        video_id = item.get("contentDetails", {}).get("videoId")
        if video_id:
            video_ids.append(video_id)

    return video_ids, data.get("nextPageToken")


def parse_iso8601_duration_to_seconds(duration_iso: str) -> int:
    try:
        duration = isodate.parse_duration(duration_iso)
        return int(duration.total_seconds())
    except Exception:
        return 0


def detect_short_form(title: str, description: str, duration_seconds: int) -> bool:
    title_lower = title.lower() if title else ""
    description_lower = description.lower() if description else ""

    if duration_seconds <= 60:
        return True

    if "#shorts" in title_lower or "#shorts" in description_lower:
        return True

    return False


def get_video_details(video_ids: List[str]) -> List[Dict]:
    if not video_ids:
        return []

    data = youtube_get(
        "videos",
        {
            "part": "snippet,contentDetails",
            "id": ",".join(video_ids),
            "maxResults": len(video_ids),
        },
    )

    videos = []

    for item in data.get("items", []):
        snippet = item.get("snippet", {})
        content_details = item.get("contentDetails", {})

        video_id = item["id"]
        title = snippet.get("title", "Untitled Video")
        description = snippet.get("description", "")
        published_at = snippet.get("publishedAt")

        duration_iso = content_details.get("duration", "PT0S")
        duration_seconds = parse_iso8601_duration_to_seconds(duration_iso)

        is_short = detect_short_form(
            title=title,
            description=description,
            duration_seconds=duration_seconds,
        )

        thumbnails = snippet.get("thumbnails", {})
        thumbnail_url = None

        for key in ["high", "medium", "default"]:
            if key in thumbnails:
                thumbnail_url = thumbnails[key].get("url")
                break

        videos.append(
            {
                "youtube_video_id": video_id,
                "title": title,
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "published_at": published_at,
                "duration_seconds": duration_seconds,
                "is_short": is_short,
                "description": description,
                "thumbnail_url": thumbnail_url,
            }
        )

    return videos


def fetch_recent_videos_for_channel(
    uploads_playlist_id: str,
    max_results: int = 20,
    longform_only: bool = True,
    max_pages: int = 10,
) -> List[Dict]:
    """업로드 플레이리스트를 페이지 단위로 순회하며 영상을 수집.

    - longform_only=True: 숏폼은 버리고, 롱폼이 max_results개 모일 때까지 페이지를 넘긴다.
    - longform_only=False: 숏폼 포함 최근 max_results개만.
    - max_pages: 폭주 방지용 페이지 상한 (페이지당 최대 50개 → 기본 500개까지 스캔).
    """
    collected: List[Dict] = []
    page_token: Optional[str] = None
    pages_scanned = 0

    while pages_scanned < max_pages:
        page_size = min(max_results - len(collected), 50) if not longform_only else 50
        if page_size <= 0:
            break

        video_ids, next_token = get_recent_video_ids_from_uploads_playlist(
            uploads_playlist_id=uploads_playlist_id,
            max_results=page_size,
            page_token=page_token,
        )
        pages_scanned += 1

        if not video_ids:
            break

        page_videos = get_video_details(video_ids)

        if longform_only:
            page_videos = [v for v in page_videos if not v.get("is_short")]

        collected.extend(page_videos)

        if len(collected) >= max_results:
            collected = collected[:max_results]
            break

        if not next_token:
            break
        page_token = next_token

    return collected


def format_duration(seconds: Optional[int]) -> str:
    if seconds is None:
        return "-"

    seconds = int(seconds)

    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    remain_seconds = seconds % 60

    if hours > 0:
        return f"{hours}시간 {minutes}분 {remain_seconds}초"

    if minutes > 0:
        return f"{minutes}분 {remain_seconds}초"

    return f"{remain_seconds}초"


def format_published_at(published_at: Optional[str]) -> str:
    if not published_at:
        return "-"
    try:
        dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return published_at


# =========================================================
# Streamlit 화면 관련 함수
# =========================================================

def handle_register_channel(channel_input: str, max_results: int, longform_only: bool = True):
    if not channel_input.strip():
        st.warning("채널 URL, @handle, 또는 channelId를 입력하세요.")
        return

    with st.spinner("채널 정보를 확인하고 영상을 가져오는 중입니다..."):
        try:
            channel = resolve_channel(channel_input)

            channel_db_id = upsert_channel(channel)

            videos = fetch_recent_videos_for_channel(
                uploads_playlist_id=channel["uploads_playlist_id"],
                max_results=max_results,
                longform_only=longform_only,
            )

            result = upsert_videos(channel_db_id, videos)
            st.cache_data.clear()

            mode = "롱폼만" if longform_only else "숏폼 포함"
            st.success(
                f"채널 등록 완료 ({mode}): {channel['title']} / 신규 {result['new']}개, 업데이트 {result['updated']}개"
            )

        except YouTubeAPIError as e:
            st.error(str(e))

        except Exception as e:
            st.error(f"예상치 못한 오류가 발생했습니다: {e}")


def handle_refresh_all_channels(max_results: int, longform_only: bool = True):
    channels = get_channels()

    if not channels:
        st.warning("등록된 채널이 없습니다.")
        return

    total_new = 0
    total_updated = 0

    progress_bar = st.progress(0)
    status_area = st.empty()

    for index, channel in enumerate(channels, start=1):
        status_area.info(f"새로고침 중: {channel['title']}")

        try:
            videos = fetch_recent_videos_for_channel(
                uploads_playlist_id=channel["uploads_playlist_id"],
                max_results=max_results,
                longform_only=longform_only,
            )

            result = upsert_videos(channel["id"], videos)
            total_new += result["new"]
            total_updated += result["updated"]

        except Exception as e:
            st.warning(f"{channel['title']} 처리 중 오류: {e}")

        progress_bar.progress(index / len(channels))

    st.cache_data.clear()
    status_area.success(
        f"전체 새로고침 완료. 신규 {total_new}개, 업데이트 {total_updated}개"
    )


def render_channel_header(channel: Dict):
    col1, col2, col3 = st.columns([1, 4, 1])

    with col1:
        if channel.get("thumbnail_url"):
            st.image(channel["thumbnail_url"], width=80)

    with col2:
        st.markdown(f"### {channel['title']}")
        st.markdown(f"[채널 바로가기]({channel['url']})")
        st.caption(f"Channel ID: `{channel['youtube_channel_id']}`")

    with col3:
        confirm_key = f"confirm_deactivate_{channel['id']}"
        if st.session_state.get(confirm_key):
            st.warning("정말 비활성화?")
            c1, c2 = st.columns(2)
            if c1.button("✓ 예", key=f"confirm_yes_{channel['id']}", type="primary"):
                delete_channel(channel["id"])
                st.session_state[confirm_key] = False
                st.cache_data.clear()
                st.rerun()
            if c2.button("취소", key=f"confirm_no_{channel['id']}"):
                st.session_state[confirm_key] = False
                st.rerun()
        else:
            if st.button("비활성화", key=f"delete_{channel['id']}"):
                st.session_state[confirm_key] = True
                st.rerun()


def handle_summarize_video(video: Dict):
    """자막 → 요약 파이프라인 실행"""
    update_video_summary_status(video["id"], "in_progress")

    with st.spinner("자막 추출 및 요약 생성 중..."):
        try:
            transcript_text, lang = fetch_transcript(video["youtube_video_id"])
        except TranscriptError as e:
            update_video_summary_status(video["id"], "failed")
            st.error(f"자막을 가져오지 못했습니다: {e}")
            return

        try:
            summary, model = summarize_video(
                title=video["title"],
                transcript=transcript_text,
                description=video.get("description") or "",
            )
        except SummarizerError as e:
            update_video_summary_status(video["id"], "failed")
            st.error(f"요약 생성에 실패했습니다: {e}")
            return

        update_video_summary(
            video_db_id=video["id"],
            summary_text=summary,
            model=model,
            transcript_text=transcript_text,
            transcript_lang=lang,
        )

    st.cache_data.clear()
    st.success("요약이 생성되었습니다.")
    st.rerun()


def render_video_detail(video: Dict):
    """영상 상세 영역: 링크 + 썸네일 + 요약/설명"""
    col1, col2 = st.columns([1, 3])

    with col1:
        if video.get("thumbnail_url"):
            st.image(video["thumbnail_url"], use_container_width=True)

    with col2:
        st.markdown(f"**업로드:** {format_published_at(video.get('published_at'))}")
        st.markdown(f"**길이:** {format_duration(video.get('duration_seconds'))}")
        st.markdown(f"[YouTube에서 열기]({video['url']})")

    st.divider()

    summary_status = video.get("summary_status") or "not_started"
    summary_text = video.get("summary_text")

    if summary_status == "done" and summary_text:
        st.markdown("##### 요약")
        if video.get("summary_model"):
            st.caption(f"모델: {video['summary_model']} · 갱신: {video.get('summary_updated_at') or '-'}")
        st.write(summary_text)

        if st.button("요약 다시 생성", key=f"resummarize_{video['id']}"):
            handle_summarize_video(video)
    else:
        st.info(
            {
                "not_started": "아직 요약이 생성되지 않았습니다.",
                "in_progress": "요약 생성 중...",
                "failed": "지난번 요약이 실패했습니다.",
            }.get(summary_status, "요약 상태를 알 수 없습니다.")
        )

        if st.button("요약 생성", key=f"summarize_{video['id']}", type="primary"):
            handle_summarize_video(video)

    with st.expander("원본 설명 보기"):
        description = video.get("description") or "설명이 없습니다."
        st.write(description[:3000])


def render_video_list(channel_db_id: int, videos: List[Dict]):
    """제목 위주 리스트. 클릭(체크) 시 하단에 상세."""
    if not videos:
        st.info("저장된 영상이 없습니다. 새로고침을 실행하세요.")
        return

    selected_key = f"selected_video_{channel_db_id}"
    selected_id = st.session_state.get(selected_key)

    for video in videos:
        is_selected = selected_id == video["id"]
        is_new = not video.get("seen")

        title_prefix = "🔵 " if is_new else ""
        published = format_published_at(video.get("published_at"))
        duration_text = format_duration(video.get("duration_seconds"))
        summary_badge = "📝" if video.get("summary_status") == "done" else ""

        label = f"{title_prefix}{summary_badge} {video['title']}"
        caption = f"{published} · {duration_text}"

        col1, col2 = st.columns([6, 1])
        with col1:
            if st.button(label, key=f"video_btn_{video['id']}", use_container_width=True):
                if is_selected:
                    st.session_state[selected_key] = None
                else:
                    st.session_state[selected_key] = video["id"]
                    if is_new:
                        mark_video_seen(video["id"])
                st.rerun()
            st.caption(caption)
        with col2:
            if is_new:
                st.caption("NEW")

        if is_selected:
            with st.container(border=True):
                fresh = get_video(video["id"]) or video
                render_video_detail(fresh)


def render_channels(include_shorts: bool = False):
    channels = get_channels()

    st.subheader("채널 / 영상")

    if not channels:
        st.info("아직 등록된 채널이 없습니다. 왼쪽에서 유튜브 채널을 등록하세요.")
        return

    stats = get_channel_video_stats(include_shorts=include_shorts)
    channel_ids = [channel["id"] for channel in channels]
    channel_by_id = {channel["id"]: channel for channel in channels}

    if st.session_state.get("selected_channel_id") not in channel_ids:
        st.session_state["selected_channel_id"] = channel_ids[0]

    def format_channel_option(channel_id: int) -> str:
        channel = channel_by_id[channel_id]
        stat = stats.get(channel_id, {"video_count": 0, "new_count": 0})
        new_label = f" · 🔵 NEW {stat['new_count']}" if stat["new_count"] else ""
        return f"{channel['title']} · 영상 {stat['video_count']}개{new_label}"

    selected_channel_id = st.selectbox(
        "채널 선택",
        options=channel_ids,
        format_func=format_channel_option,
        key="selected_channel_id",
    )

    selected_channel = channel_by_id[selected_channel_id]
    render_channel_header(selected_channel)

    videos = get_videos_by_channel(
        channel_db_id=selected_channel_id,
        include_shorts=include_shorts,
    )
    render_video_list(selected_channel_id, videos)


def render_inactive_channels():
    """비활성화된 채널 목록 + 재활성화 버튼"""
    inactive = get_inactive_channels()
    if not inactive:
        st.info("비활성화된 채널이 없습니다.")
        return

    st.subheader(f"💤 비활성 채널 ({len(inactive)}개)")
    st.caption("비활성 채널은 워커가 더 이상 조회하지 않습니다. 재활성화하면 다시 모니터링됩니다.")

    for channel in inactive:
        with st.container(border=True):
            col1, col2, col3 = st.columns([1, 4, 1])
            with col1:
                if channel.get("thumbnail_url"):
                    st.image(channel["thumbnail_url"], width=60)
            with col2:
                st.markdown(f"**{channel['title']}**")
                st.caption(f"[채널 바로가기]({channel['url']}) · `{channel['youtube_channel_id']}`")
            with col3:
                if st.button("재활성화", key=f"reactivate_{channel['id']}", type="primary"):
                    reactivate_channel(channel["id"])
                    st.cache_data.clear()
                    st.rerun()


def render_notifications():
    unseen = get_unseen_videos(limit=20)

    if not unseen:
        return

    st.subheader(f"🔔 새 영상 알림 ({len(unseen)}개)")

    for v in unseen[:10]:
        with st.container(border=True):
            st.markdown(f"**[{v['channel_title']}]** {v['title']}")
            st.caption(
                f"{format_published_at(v.get('published_at'))} · {format_duration(v.get('duration_seconds'))} · [열기]({v['url']})"
            )


def render_trends_tab():
    st.subheader("AI 트렌드 요약")

    period_mode = st.radio(
        "기간 단위",
        options=["주별", "월별"],
        horizontal=True,
        key="trend_period_mode",
    )

    now = datetime.utcnow()
    if period_mode == "주별":
        start = now - timedelta(days=7)
        cache_key = "trend_summary_week"
    else:
        start = now - timedelta(days=30)
        cache_key = "trend_summary_month"

    videos = get_videos_in_period(
        start_iso=start.isoformat() + "Z",
        end_iso=(now + timedelta(days=1)).isoformat() + "Z",
        include_shorts=False,
    )

    if not videos:
        st.info("해당 기간에 수집된 롱폼 영상이 없습니다.")
        return

    st.caption(
        f"**기간:** {start.strftime('%Y-%m-%d')} ~ {now.strftime('%Y-%m-%d')} · 총 {len(videos)}개 영상"
    )

    col_a, col_b = st.columns([1, 4])
    with col_a:
        clicked = st.button("요약 생성", key=f"trend_btn_{cache_key}", type="primary")
    with col_b:
        cached = st.session_state.get(cache_key)
        if cached:
            st.caption(f"마지막 생성: {cached.get('generated_at', '-')} (모델: {cached.get('model', '-')})")

    if clicked:
        with st.spinner("Gemini로 트렌드 분석 중..."):
            try:
                from summarizer import summarize_trend
                summary, model = summarize_trend(videos)
                st.session_state[cache_key] = {
                    "summary": summary,
                    "model": model,
                    "generated_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
                }
                st.rerun()
            except SummarizerError as e:
                st.error(f"트렌드 요약 실패: {e}")
            except Exception as e:
                st.error(f"오류: {type(e).__name__}: {e}")

    cached = st.session_state.get(cache_key)
    if cached:
        st.markdown("---")
        st.markdown(cached["summary"])


# =========================================================
# 메인 앱
# =========================================================

def _get_expected_password() -> str:
    """환경변수 또는 st.secrets에서 비밀번호 조회. 둘 다 없으면 빈 문자열."""
    val = os.getenv("APP_PASSWORD", "")
    if val:
        return val
    try:
        return st.secrets.get("APP_PASSWORD", "") or ""
    except Exception:
        return ""


def _check_password() -> bool:
    """Streamlit Cloud 등 공개 환경에서 단일 비밀번호 게이트.
    APP_PASSWORD가 비어있으면 게이트를 건너뛴다 (로컬 사용 시).
    """
    expected = _get_expected_password()
    if not expected:
        return True

    if st.session_state.get("auth_ok"):
        return True

    st.title("🔒 YouTube Monitor")
    st.caption("비밀번호를 입력하세요.")
    pwd = st.text_input("Password", type="password", key="pwd_input")
    if st.button("로그인"):
        if pwd == expected:
            st.session_state["auth_ok"] = True
            st.rerun()
        else:
            st.error("비밀번호가 일치하지 않습니다.")
    return False


def main():
    st.set_page_config(
        page_title="YouTube Channel Monitor",
        page_icon="🎥",
        layout="wide",
    )

    if not _check_password():
        return

    init_db()

    st.title("🎥 YouTube Channel Monitor MVP")

    st.caption(
        "유튜브 채널을 등록하고, 최근 업로드된 롱폼 영상을 모니터링·요약·분석하는 로컬 MVP."
    )

    with st.sidebar:
        st.header("채널 등록")

        channel_input = st.text_input(
            "YouTube 채널 URL / @handle / channelId",
            placeholder="예: https://www.youtube.com/@GCL 또는 @GCL",
        )

        max_results = st.slider(
            "가져올 최근 영상 수",
            min_value=5,
            max_value=200,
            value=30,
            step=5,
        )

        longform_only_fetch = st.checkbox(
            "롱폼만 가져오기 (숏폼은 DB에도 저장 안 함)",
            value=True,
            help="체크 시: 숏폼을 건너뛰고 롱폼이 위 개수만큼 모일 때까지 페이지를 더 가져옵니다.",
        )

        register_clicked = st.button(
            "채널 등록 및 영상 가져오기",
            use_container_width=True,
        )

        st.divider()

        include_shorts = st.checkbox(
            "숏폼 포함해서 보기",
            value=False,
        )

        show_inactive = st.checkbox(
            "비활성 채널 보기",
            value=False,
            help="비활성화한 채널 목록을 보고 재활성화할 수 있습니다.",
        )

        refresh_all_clicked = st.button(
            "전체 채널 영상 새로고침",
            use_container_width=True,
        )

        st.divider()

        st.markdown("### 현재 버전")
        st.caption("v0.2")
        st.caption("리스트 분리 / 요약(Gemini) / 알림 / 트렌드")

    if register_clicked:
        handle_register_channel(channel_input, max_results, longform_only_fetch)

    if refresh_all_clicked:
        handle_refresh_all_channels(max_results, longform_only_fetch)

    tab_channels, tab_notifications, tab_trends = st.tabs(
        ["📺 채널 / 영상", "🔔 알림", "📊 트렌드"]
    )

    with tab_channels:
        render_channels(include_shorts=include_shorts)
        if show_inactive:
            st.divider()
            render_inactive_channels()

    with tab_notifications:
        render_notifications()

    with tab_trends:
        render_trends_tab()


if __name__ == "__main__":
    main()
