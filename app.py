import csv
import io
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
from github_sync import sync_db_to_github, is_github_sync_configured, GitHubSyncError


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
    _ensure_column(cur, "videos", "summary_retry_count", "INTEGER DEFAULT 0")
    _ensure_column(cur, "channels", "notify_enabled", "INTEGER DEFAULT 1")

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )

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


def set_channel_notify(channel_db_id: int, enabled: bool):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE channels SET notify_enabled = ? WHERE id = ?",
        (1 if enabled else 0, channel_db_id),
    )
    conn.commit()
    conn.close()


# ===== Settings (key-value) =====

def get_setting(key: str, default: str = "") -> str:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cur.fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key: str, value: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )
    conn.commit()
    conn.close()


def get_int_setting(key: str, default: int) -> int:
    try:
        return int(get_setting(key, str(default)))
    except Exception:
        return default


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


@st.cache_data(ttl=30)
def search_videos(query: str, include_shorts: bool = False, limit: int = 100) -> List[Dict]:
    """제목·요약·description에서 키워드 검색. 최신순 limit개."""
    if not query or not query.strip():
        return []
    q = f"%{query.strip()}%"
    conn = get_connection()
    cur = conn.cursor()
    short_filter = "" if include_shorts else " AND v.is_short = 0"
    cur.execute(
        f"""
        SELECT v.id, v.youtube_video_id, v.title, v.url, v.published_at,
               v.duration_seconds, v.is_short, v.thumbnail_url,
               v.summary_status, v.summary_text, v.summary_model,
               c.title AS channel_title, c.id AS channel_db_id
        FROM videos v
        JOIN channels c ON c.id = v.channel_db_id
        WHERE c.active = 1
          {short_filter}
          AND (
            v.title LIKE ?
            OR v.summary_text LIKE ?
            OR v.description LIKE ?
          )
        ORDER BY v.published_at DESC
        LIMIT ?
        """,
        (q, q, q, limit),
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

def sync_db_after_change(reason: str, show_success: bool = False):
    """Streamlit Cloud에서 발생한 DB 변경을 GitHub repo DB로 반영.

    REPO_SYNC_TOKEN이 없으면 기존 로컬 동작만 유지하고 조용히 건너뛴다.
    """
    if not is_github_sync_configured():
        return

    with st.spinner("GitHub 저장소 DB 동기화 중..."):
        try:
            synced, message = sync_db_to_github(DB_PATH, reason)
        except GitHubSyncError as e:
            st.warning(f"DB는 앱에 반영됐지만 GitHub 동기화는 실패했습니다: {e}")
            return

    if synced and show_success:
        st.caption(message)


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
            sync_db_after_change(f"register channel: {channel['title']}")

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
    sync_db_after_change("manual refresh all channels")
    status_area.success(
        f"전체 새로고침 완료. 신규 {total_new}개, 업데이트 {total_updated}개"
    )


def render_channel_header(channel: Dict):
    # 호버 / 슬라이드 인터랙션용 CSS
    st.markdown(
        """
        <style>
        .ch-link {
            color: #1a73e8;
            text-decoration: none;
            font-weight: 500;
            position: relative;
            transition: color 0.15s ease;
            padding-bottom: 2px;
        }
        .ch-link:hover { color: #0d47a1; }
        .ch-link::after {
            content: '';
            position: absolute;
            left: 0;
            bottom: 0;
            width: 0;
            height: 1.5px;
            background: #0d47a1;
            transition: width 0.25s ease;
        }
        .ch-link:hover::after { width: 100%; }
        .ch-link .arrow { display:inline-block; transition: transform 0.2s ease; }
        .ch-link:hover .arrow { transform: translate(3px, -2px); }

        /* Channel ID 슬라이드 토글 */
        .id-toggle { display: none; }
        .id-label {
            cursor: pointer;
            color: #888;
            font-size: 0.82rem;
            padding: 1px 8px;
            border: 1px solid #ddd;
            border-radius: 999px;
            user-select: none;
            transition: background 0.15s ease, color 0.15s ease, border-color 0.15s ease;
            vertical-align: middle;
        }
        .id-label:hover { background: #f5f5f5; color: #555; border-color: #bbb; }
        .id-toggle:checked + .id-label {
            background: #e8f0fe;
            color: #1a73e8;
            border-color: #1a73e8;
        }
        .id-value {
            display: inline-block;
            max-width: 0;
            overflow: hidden;
            white-space: nowrap;
            vertical-align: middle;
            opacity: 0;
            color: #888;
            font-family: ui-monospace, Menlo, monospace;
            font-size: 0.82rem;
            margin-left: 0;
            transition:
                max-width 0.45s cubic-bezier(0.4, 0, 0.2, 1),
                margin-left 0.45s cubic-bezier(0.4, 0, 0.2, 1),
                opacity 0.3s ease 0.05s;
        }
        .id-toggle:checked + .id-label + .id-value {
            max-width: 600px;
            margin-left: 8px;
            opacity: 1;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    # 3컬럼: [썸네일] [정보] [비활성화]
    col_thumb, col_info, col_btn = st.columns(
        [1, 7.5, 1.5], vertical_alignment="center", gap="small"
    )

    with col_thumb:
        thumb = channel.get("thumbnail_url") or ""
        if thumb:
            st.markdown(
                f'<img src="{thumb}" style="width:100%; aspect-ratio:1/1; '
                f'object-fit:cover; border-radius:10px; display:block;" />',
                unsafe_allow_html=True,
            )

    with col_info:
        # Line 1: 채널명
        st.markdown(
            f"""
            <div style="font-size:1.4rem; font-weight:700; line-height:1.25;
                        white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
                        margin:0; padding:0;">
              {channel['title']}
            </div>
            """,
            unsafe_allow_html=True,
        )

        # Line 2: 채널 바로가기 + Channel ID 슬라이드 토글
        cid = channel['youtube_channel_id']
        toggle_id = f"id-toggle-{channel['id']}"
        st.markdown(
            f"""
            <div style="margin-top:-0.1rem; line-height:1.25; font-size:0.92rem;">
              <a href="{channel['url']}" target="_blank" class="ch-link">
                채널 바로가기 <span class="arrow">↗</span>
              </a>
              &nbsp;·&nbsp;
              <input type="checkbox" id="{toggle_id}" class="id-toggle">
              <label for="{toggle_id}" class="id-label">Channel ID</label>
              <span class="id-value">{cid}</span>
            </div>
            """,
            unsafe_allow_html=True,
        )

        # Line 3: 알림 토글
        current_notify = bool(channel.get("notify_enabled", 1))
        new_notify = st.toggle(
            "🔔 신규 영상 알림",
            value=current_notify,
            key=f"notify_toggle_{channel['id']}",
            help="끄면 이 채널의 신규 영상이 와도 텔레그램/메일 알림을 보내지 않습니다 (UI에는 여전히 표시됨).",
        )
        if new_notify != current_notify:
            set_channel_notify(channel["id"], new_notify)
            st.cache_data.clear()
            sync_db_after_change(
                f"{'enable' if new_notify else 'disable'} notify: {channel['title']}"
            )
            st.rerun()

    with col_btn:
        confirm_key = f"confirm_deactivate_{channel['id']}"
        if st.session_state.get(confirm_key):
            cc1, cc2 = st.columns(2)
            if cc1.button("✓", key=f"confirm_yes_{channel['id']}",
                          type="primary", help="비활성화 확정"):
                delete_channel(channel["id"])
                st.session_state[confirm_key] = False
                st.cache_data.clear()
                sync_db_after_change(f"deactivate channel: {channel['title']}")
                st.rerun()
            if cc2.button("✕", key=f"confirm_no_{channel['id']}", help="취소"):
                st.session_state[confirm_key] = False
                st.rerun()
        else:
            if st.button("비활성화", key=f"delete_{channel['id']}",
                         use_container_width=True):
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
    sync_db_after_change(f"manual summarize video: {video['title']}")
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


def render_channel_thumbnail_grid(
    channels: List[Dict],
    stats: Dict[int, Dict[str, int]],
    selected_channel_id: int,
    cols_per_row: int = 6,
):
    """채널 썸네일 그리드. 클릭 시 selected_channel_id 변경. 선택된 채널은 빨강 테두리."""
    for i in range(0, len(channels), cols_per_row):
        row = channels[i:i + cols_per_row]
        cols = st.columns(cols_per_row)
        for j, channel in enumerate(row):
            with cols[j]:
                stat = stats.get(channel["id"], {"video_count": 0, "new_count": 0})
                is_selected = channel["id"] == selected_channel_id
                border_color = "#FF3B30" if is_selected else "transparent"
                thumb = channel.get("thumbnail_url") or ""

                # 썸네일 원형 + 선택 시 빨강 테두리
                st.markdown(
                    f"""
                    <div style="text-align:center;">
                      <div style="display:inline-block; padding:3px; border-radius:50%;
                                  border:3px solid {border_color}; background:{border_color};">
                        {'<img src="' + thumb + '" style="width:72px; height:72px; border-radius:50%; object-fit:cover; display:block;" />'
                         if thumb else '<div style="width:72px;height:72px;border-radius:50%;background:#ddd;"></div>'}
                      </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

                # 채널명 + NEW 배지
                title = channel["title"]
                if len(title) > 8:
                    title_display = title[:7] + "…"
                else:
                    title_display = title
                new_count = stat["new_count"]
                btn_label = f"🔵 {title_display}" if new_count else title_display
                if st.button(
                    btn_label,
                    key=f"thumb_select_{channel['id']}",
                    use_container_width=True,
                    type="primary" if is_selected else "secondary",
                    help=f"{title} · 영상 {stat['video_count']}개" + (f" · NEW {new_count}" if new_count else ""),
                ):
                    if st.session_state.get("selected_channel_id") != channel["id"]:
                        st.session_state["selected_channel_id"] = channel["id"]
                        st.rerun()


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

    # 1) 썸네일 그리드 — 빠른 시각 선택
    render_channel_thumbnail_grid(channels, stats, st.session_state["selected_channel_id"])

    # 2) 셀렉트박스 — 키보드/접근성용 대체 입력
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

    # 채널 헤더와 영상 리스트 사이 여백
    st.markdown(
        '<div style="height:18px;"></div>',
        unsafe_allow_html=True,
    )

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
                    sync_db_after_change(f"reactivate channel: {channel['title']}")
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


def videos_to_csv(videos: List[Dict]) -> bytes:
    """영상 리스트 → UTF-8 BOM CSV bytes (엑셀 한글 호환)"""
    if not videos:
        return b""
    fields = [
        "channel_title", "title", "published_at", "duration_seconds",
        "url", "summary_text", "summary_status",
    ]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for v in videos:
        writer.writerow({f: v.get(f, "") for f in fields})
    return ("﻿" + buf.getvalue()).encode("utf-8")


def render_search_tab(include_shorts: bool = False):
    st.subheader("🔍 영상 검색")
    st.caption("제목·요약·설명에서 키워드 검색합니다.")

    query = st.text_input(
        "검색어",
        placeholder="예: 발로란트, AI, 인디게임",
        key="search_query",
    )

    if not query or not query.strip():
        st.info("검색어를 입력하세요.")
        return

    results = search_videos(query=query, include_shorts=include_shorts, limit=100)

    if not results:
        st.warning(f"'{query}'에 해당하는 영상이 없습니다.")
        return

    col_a, col_b = st.columns([4, 1])
    with col_a:
        st.caption(f"검색 결과: **{len(results)}개**")
    with col_b:
        st.download_button(
            "📥 CSV 다운로드",
            data=videos_to_csv(results),
            file_name=f"search_{query.strip()[:20]}_{datetime.utcnow().strftime('%Y%m%d')}.csv",
            mime="text/csv",
            use_container_width=True,
        )

    for v in results:
        with st.container(border=True):
            col1, col2 = st.columns([1, 4])
            with col1:
                if v.get("thumbnail_url"):
                    st.image(v["thumbnail_url"], use_container_width=True)
            with col2:
                st.markdown(f"**[{v['channel_title']}]** {v['title']}")
                st.caption(
                    f"{format_published_at(v.get('published_at'))} · "
                    f"{format_duration(v.get('duration_seconds'))} · "
                    f"[YouTube에서 열기]({v['url']})"
                )
                summary = v.get("summary_text") or ""
                if summary:
                    with st.expander("요약 보기"):
                        st.write(summary)
                else:
                    st.caption(f"요약 없음 (status: {v.get('summary_status') or 'not_started'})")


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

    col_a, col_b, col_c = st.columns([1, 1, 3])
    with col_a:
        clicked = st.button("요약 생성", key=f"trend_btn_{cache_key}", type="primary")
    with col_b:
        st.download_button(
            "📥 CSV",
            data=videos_to_csv(videos),
            file_name=f"trend_{period_mode}_{datetime.utcnow().strftime('%Y%m%d')}.csv",
            mime="text/csv",
            key=f"trend_csv_{cache_key}",
        )
    with col_c:
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

    col_logo, col_title = st.columns([1, 9])
    with col_logo:
        logo_path = Path(__file__).parent / "assets" / "logo.png"
        if logo_path.exists():
            st.image(str(logo_path), width=80)
    with col_title:
        st.markdown(
            """
            <div style="display:flex; align-items:baseline; gap:14px; flex-wrap:wrap; margin-top:8px;">
              <span style="font-size:2.5rem; font-weight:700;">🔒 Game YouTube Monitoring</span>
              <span style="font-size:1.6rem; color:#6e6e6e; font-weight:500;">made by Jaykim</span>
            </div>
            """,
            unsafe_allow_html=True,
        )
    pwd = st.text_input("Password", type="password", key="pwd_input")
    if st.button("로그인"):
        if pwd == expected:
            st.session_state["auth_ok"] = True
            st.rerun()
        else:
            st.error("비밀번호가 일치하지 않습니다.")
    return False


PWA_MANIFEST_INLINE = """
<link rel="manifest" href="data:application/json;base64,eyJuYW1lIjoiWVQgTW9uaXRvciIsInNob3J0X25hbWUiOiJZVCBNb25pdG9yIiwic3RhcnRfdXJsIjoiLi8iLCJkaXNwbGF5Ijoic3RhbmRhbG9uZSIsImJhY2tncm91bmRfY29sb3IiOiIjMGUxMTE3IiwidGhlbWVfY29sb3IiOiIjRkYwMDAwIiwiaWNvbnMiOlt7InNyYyI6Imh0dHBzOi8vd3d3LnlvdXR1YmUuY29tL2Zhdmljb24uaWNvIiwic2l6ZXMiOiI2NHg2NCIsInR5cGUiOiJpbWFnZS9pY28ifV19">
<meta name="theme-color" content="#FF0000">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="YT Monitor">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<link rel="apple-touch-icon" href="https://www.youtube.com/s/desktop/d743f8e9/img/favicon_144x144.png">
"""


def main():
    st.set_page_config(
        page_title="Game YouTube Monitoring",
        page_icon=str(Path(__file__).parent / "assets" / "logo.png")
            if (Path(__file__).parent / "assets" / "logo.png").exists()
            else "🎮",
        layout="wide",
    )

    # PWA 매니페스트 + 모바일 메타 주입 (홈화면 추가 시 더 앱처럼 보임)
    st.markdown(PWA_MANIFEST_INLINE, unsafe_allow_html=True)

    if not _check_password():
        return

    init_db()

    col_logo, col_title = st.columns([1, 9])
    with col_logo:
        logo_path = Path(__file__).parent / "assets" / "logo.png"
        if logo_path.exists():
            st.image(str(logo_path), width=80)
    with col_title:
        st.markdown(
            """
            <div style="display:flex; align-items:baseline; gap:14px; flex-wrap:wrap; margin-top:8px;">
              <span style="font-size:2.5rem; font-weight:700;">Game YouTube Monitoring</span>
              <span style="font-size:1.6rem; color:#6e6e6e; font-weight:500;">made by Jaykim</span>
            </div>
            """,
            unsafe_allow_html=True,
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

        with st.expander("🔔 알림 설정", expanded=False):
            current_mode = get_setting("notify_mode", "instant")
            mode = st.radio(
                "알림 모드",
                options=["instant", "digest"],
                format_func=lambda x: "즉시 발송" if x == "instant" else "하루 1회 다이제스트",
                index=0 if current_mode == "instant" else 1,
                key="notify_mode_radio",
                help="다이제스트: 정해진 시간에 그 사이 누적된 신규 영상을 모아 한 번에 발송",
            )
            if mode != current_mode:
                set_setting("notify_mode", mode)
                st.cache_data.clear()
                sync_db_after_change(f"set notify_mode={mode}")
                st.rerun()

            quiet_on = get_int_setting("quiet_hours_enabled", 0) == 1
            quiet_on_new = st.checkbox("조용 시간 사용", value=quiet_on, key="quiet_on")
            if quiet_on_new != quiet_on:
                set_setting("quiet_hours_enabled", "1" if quiet_on_new else "0")
                sync_db_after_change(f"set quiet_hours_enabled={quiet_on_new}")
                st.rerun()

            if quiet_on_new:
                qs = get_int_setting("quiet_start_kst", 23)
                qe = get_int_setting("quiet_end_kst", 7)
                col_qs, col_qe = st.columns(2)
                qs_new = col_qs.number_input(
                    "조용 시작 (KST, 시)", min_value=0, max_value=23, value=qs, key="qs_inp"
                )
                qe_new = col_qe.number_input(
                    "조용 종료 (KST, 시)", min_value=0, max_value=23, value=qe, key="qe_inp"
                )
                if qs_new != qs or qe_new != qe:
                    set_setting("quiet_start_kst", str(int(qs_new)))
                    set_setting("quiet_end_kst", str(int(qe_new)))
                    sync_db_after_change(f"set quiet_hours {qs_new}-{qe_new} KST")
                    st.rerun()

            if mode == "digest":
                dh = get_int_setting("digest_hour_kst", 9)
                dh_new = st.number_input(
                    "다이제스트 발송 시각 (KST, 시)",
                    min_value=0, max_value=23, value=dh, key="dh_inp",
                )
                if dh_new != dh:
                    set_setting("digest_hour_kst", str(int(dh_new)))
                    sync_db_after_change(f"set digest_hour={dh_new}")
                    st.rerun()

            pending = get_int_setting("pending_notify_count", 0)
            if pending:
                st.caption(f"⏳ 대기 중 알림: {pending}개")

        with st.expander("🗂️ 데이터 보존", expanded=False):
            retention_on = get_int_setting("retention_enabled", 0) == 1
            retention_on_new = st.checkbox(
                "오래된 영상 자동 정리",
                value=retention_on,
                key="retention_on",
                help="설정한 개월 수보다 오래되고, 이미 알림 발송된 영상을 워커 실행 시 자동 삭제.",
            )
            if retention_on_new != retention_on:
                set_setting("retention_enabled", "1" if retention_on_new else "0")
                sync_db_after_change(f"set retention_enabled={retention_on_new}")
                st.rerun()
            if retention_on_new:
                months = get_int_setting("data_retention_months", 12)
                months_new = st.number_input(
                    "보존 기간 (개월)",
                    min_value=1, max_value=60, value=months, key="ret_months",
                )
                if months_new != months:
                    set_setting("data_retention_months", str(int(months_new)))
                    sync_db_after_change(f"set retention={months_new} months")
                    st.rerun()

        st.divider()

        st.markdown("### 현재 버전")
        st.caption("v0.3")
        st.caption("실패 재시도 / 검색 / 채널 알림 / 조용시간 / 다이제스트")

    if register_clicked:
        handle_register_channel(channel_input, max_results, longform_only_fetch)

    if refresh_all_clicked:
        handle_refresh_all_channels(max_results, longform_only_fetch)

    tab_channels, tab_search, tab_notifications, tab_trends = st.tabs(
        ["📺 채널 / 영상", "🔍 검색", "🔔 알림", "📊 트렌드"]
    )

    with tab_channels:
        if show_inactive:
            render_inactive_channels()
            st.divider()
        render_channels(include_shorts=include_shorts)

    with tab_search:
        render_search_tab(include_shorts=include_shorts)

    with tab_notifications:
        render_notifications()

    with tab_trends:
        render_trends_tab()


if __name__ == "__main__":
    main()
