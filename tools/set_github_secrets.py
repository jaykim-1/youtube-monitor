"""Sync .env -> GitHub repo secrets using gh CLI.

- Reads .env with python-dotenv (handles quoting, comments, encodings)
- Skips empty values
- Skips placeholder values from .env.example
- Pipes values to `gh secret set` via stdin (never appears in shell history)
- Deletes secrets that we no longer want set (e.g. placeholder SMTP)
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from dotenv import dotenv_values

REPO = "jaykim-1/youtube-monitor"
ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"

REQUIRED_KEYS = [
    "YOUTUBE_API_KEY",
    "GOOGLE_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
]

# Optional (SMTP). Uploaded only when value looks real (not placeholder).
OPTIONAL_KEYS = [
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USER",
    "SMTP_PASSWORD",
    "SMTP_FROM",
    "NOTIFY_TO",
]

PLACEHOLDERS = {
    "your_gmail@gmail.com",
    "app_password_here",
    "jaykim@sooplive.com",  # treat default NOTIFY_TO as placeholder until user confirms
}


def find_gh() -> str:
    gh = shutil.which("gh")
    if gh:
        return gh
    candidate = Path(
        r"C:\Users\user\AppData\Local\Microsoft\WinGet\Packages"
        r"\GitHub.cli_Microsoft.Winget.Source_8wekyb3d8bbwe\bin\gh.exe"
    )
    if candidate.exists():
        return str(candidate)
    raise SystemExit("gh CLI not found. Install or add to PATH.")


def is_placeholder(value: str) -> bool:
    if not value:
        return True
    return value.strip() in PLACEHOLDERS


def gh_secret_set(gh: str, name: str, value: str) -> bool:
    proc = subprocess.run(
        [gh, "secret", "set", name, "--repo", REPO],
        input=value,
        text=True,
        capture_output=True,
    )
    if proc.returncode == 0:
        return True
    print(f"[FAIL] {name}: {proc.stderr.strip()}", file=sys.stderr)
    return False


def gh_secret_delete(gh: str, name: str) -> None:
    subprocess.run(
        [gh, "secret", "delete", name, "--repo", REPO],
        capture_output=True,
    )


def main():
    if not ENV_FILE.exists():
        raise SystemExit(f".env not found at {ENV_FILE}")
    gh = find_gh()
    values = dotenv_values(ENV_FILE)

    print(f"Target repo: {REPO}\n")

    ok = True
    for key in REQUIRED_KEYS:
        val = values.get(key) or ""
        if not val:
            print(f"[SKIP] {key}: missing in .env (REQUIRED)")
            ok = False
            continue
        if gh_secret_set(gh, key, val):
            print(f"[OK]   {key}")

    print()
    for key in OPTIONAL_KEYS:
        val = values.get(key) or ""
        if not val:
            print(f"[SKIP] {key}: not set")
            gh_secret_delete(gh, key)
            continue
        if is_placeholder(val):
            print(f"[SKIP] {key}: placeholder value, not uploading")
            gh_secret_delete(gh, key)
            continue
        if gh_secret_set(gh, key, val):
            print(f"[OK]   {key}")

    print()
    if ok:
        print("All required secrets are present.")
    else:
        print("Some required secrets are missing.")
        sys.exit(1)


if __name__ == "__main__":
    main()
