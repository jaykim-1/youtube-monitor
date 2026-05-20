"""GitHub Contents API 기반 SQLite DB 동기화.

Streamlit Cloud에서 DB를 수정하면 컨테이너 내부 파일만 바뀌므로,
REPO_SYNC_TOKEN이 설정된 경우 youtube_monitor.db를 저장소에도 커밋한다.
"""

import base64
import os
from datetime import datetime
from pathlib import Path
from typing import Tuple

import requests
from dotenv import load_dotenv

load_dotenv()


class GitHubSyncError(Exception):
    pass


def _read_secret(name: str, default: str = "") -> str:
    val = os.getenv(name, "")
    if val:
        return val
    try:
        import streamlit as st  # type: ignore

        return st.secrets.get(name, default) or default
    except Exception:
        return default


REPO_SYNC_TOKEN = _read_secret("REPO_SYNC_TOKEN")
GITHUB_REPO = _read_secret("GITHUB_REPO", "jaykim-1/youtube-monitor")
GITHUB_BRANCH = _read_secret("GITHUB_BRANCH", "main")
DB_REPO_PATH = _read_secret("DB_REPO_PATH", "youtube_monitor.db")


def is_github_sync_configured() -> bool:
    return bool(REPO_SYNC_TOKEN and GITHUB_REPO and GITHUB_BRANCH and DB_REPO_PATH)


def sync_db_to_github(db_path: Path, reason: str) -> Tuple[bool, str]:
    """DB 파일을 GitHub 저장소에 커밋한다.

    Returns:
        (synced, message)
        synced=False는 설정이 없어 건너뛴 경우이며 오류는 아니다.
    """
    if not is_github_sync_configured():
        return False, "REPO_SYNC_TOKEN 미설정"

    if not db_path.exists():
        raise GitHubSyncError(f"DB 파일을 찾을 수 없습니다: {db_path}")

    headers = {
        "Authorization": f"Bearer {REPO_SYNC_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{DB_REPO_PATH}"

    try:
        current = requests.get(
            api_url,
            params={"ref": GITHUB_BRANCH},
            headers=headers,
            timeout=20,
        )
    except Exception as e:
        raise GitHubSyncError(f"GitHub 현재 DB 조회 실패: {e}") from e

    sha = None
    if current.status_code == 200:
        sha = current.json().get("sha")
    elif current.status_code != 404:
        raise GitHubSyncError(
            f"GitHub 현재 DB 조회 실패: {current.status_code} {current.text[:300]}"
        )

    content_b64 = base64.b64encode(db_path.read_bytes()).decode("ascii")
    timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {
        "message": f"[app] {reason} ({timestamp})",
        "content": content_b64,
        "branch": GITHUB_BRANCH,
    }
    if sha:
        payload["sha"] = sha

    try:
        update = requests.put(api_url, json=payload, headers=headers, timeout=30)
    except Exception as e:
        raise GitHubSyncError(f"GitHub DB 업데이트 실패: {e}") from e

    if update.status_code not in (200, 201):
        raise GitHubSyncError(
            f"GitHub DB 업데이트 실패: {update.status_code} {update.text[:300]}"
        )

    return True, "GitHub DB 동기화 완료"
