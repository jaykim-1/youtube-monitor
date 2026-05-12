"""신규 영상 이메일 알림 모듈 (SMTP)"""

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import List, Dict

from dotenv import load_dotenv

load_dotenv()

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER)
NOTIFY_TO = os.getenv("NOTIFY_TO", "")


class NotifierError(Exception):
    pass


def is_email_configured() -> bool:
    return bool(SMTP_USER and SMTP_PASSWORD and NOTIFY_TO)


def send_new_videos_email(new_videos: List[Dict]) -> None:
    if not new_videos:
        return

    if not is_email_configured():
        raise NotifierError(
            "SMTP 설정이 부족합니다. .env에 SMTP_USER, SMTP_PASSWORD, NOTIFY_TO를 설정하세요."
        )

    subject = f"[YouTube Monitor] 신규 롱폼 영상 {len(new_videos)}개"

    html_body = _render_html(new_videos)
    text_body = _render_text(new_videos)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = NOTIFY_TO

    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_FROM, [NOTIFY_TO], msg.as_string())
    except Exception as e:
        raise NotifierError(f"이메일 전송 실패: {e}")


def _render_text(videos: List[Dict]) -> str:
    lines = [f"신규 롱폼 영상 {len(videos)}개\n"]
    for v in videos:
        lines.append(f"- [{v.get('channel_title', '?')}] {v.get('title', '')}")
        lines.append(f"  {v.get('url', '')}")
        lines.append("")
    return "\n".join(lines)


def _render_html(videos: List[Dict]) -> str:
    rows = []
    for v in videos:
        title = v.get("title", "")
        channel = v.get("channel_title", "?")
        url = v.get("url", "#")
        published = (v.get("published_at") or "")[:16].replace("T", " ")
        thumb = v.get("thumbnail_url") or ""
        summary = (v.get("summary_text") or "").strip()
        # 마크다운 → 간단 HTML (줄바꿈/굵게)
        summary_html = ""
        if summary:
            esc = summary.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            esc = esc.replace("\n", "<br/>")
            # **굵게** → <b>
            import re as _re
            esc = _re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", esc)
            summary_html = (
                f'<div style="margin-top:8px;padding:10px;background:#f7f7f9;'
                f'border-left:3px solid #1a73e8;font-size:13px;line-height:1.6;">{esc}</div>'
            )
        rows.append(
            f"""
            <tr>
              <td style="padding:8px;vertical-align:top;width:160px;">
                {'<img src="' + thumb + '" width="150" />' if thumb else ''}
              </td>
              <td style="padding:8px;vertical-align:top;">
                <div style="font-size:14px;color:#666;">{channel} · {published}</div>
                <div style="font-size:16px;font-weight:bold;margin:4px 0;">
                  <a href="{url}" style="color:#1a73e8;text-decoration:none;">{title}</a>
                </div>
                {summary_html}
              </td>
            </tr>
            """
        )
    body = "\n".join(rows)
    return f"""
    <html><body style="font-family:Arial,sans-serif;">
      <h2>신규 롱폼 영상 {len(videos)}개</h2>
      <table cellspacing="0" cellpadding="0" style="border-collapse:collapse;width:100%;">
        {body}
      </table>
      <p style="font-size:12px;color:#999;margin-top:24px;">
        YouTube Channel Monitor · 자동 발송
      </p>
    </body></html>
    """
