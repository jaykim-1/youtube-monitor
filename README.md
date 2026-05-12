# YouTube Channel Monitor

유튜브 채널을 등록하고, 신규 롱폼 영상을 자동 감지·요약·메일 알림·트렌드 분석하는 로컬 Streamlit 앱.

## 기능

- 유튜브 채널 등록 (URL / @handle / channelId 모두 지원)
- 채널별 영상 리스트 토글, 제목 클릭 시 상세 + 링크 + 요약 표시
- 자막(`youtube-transcript-api`) → Google Gemini 요약 파이프라인
- 롱폼/숏폼 자동 분리
- 워커 스크립트 + Windows 작업 스케줄러로 주기적 신규 영상 체크
- 신규 영상 발견 시 이메일 자동 발송 + 앱 내 NEW 배지/알림 탭
- 주별/월별 업로드 빈도·키워드 빈도·AI 트렌드 요약 대시보드

## 폴더 구조

```
youtube-monitor/
├─ app.py                  # Streamlit 메인 앱
├─ worker.py               # 주기 실행 워커 (작업 스케줄러용)
├─ transcript.py           # 자막 추출
├─ summarizer.py           # Gemini 요약
├─ notifier.py             # SMTP 이메일
├─ register_scheduler.ps1  # 작업 스케줄러 등록 스크립트
├─ requirements.txt
├─ .env.example
└─ youtube_monitor.db      # SQLite (실행 시 자동 생성)
```

## 설치

```powershell
cd C:\Users\user\Desktop\youtube-monitor
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 환경 변수

`.env.example`을 `.env`로 복사 후 키 입력:

```powershell
Copy-Item .env.example .env
notepad .env
```

필요한 키:
- `YOUTUBE_API_KEY` — Google Cloud Console에서 YouTube Data API v3 활성화 후 발급
- `GOOGLE_API_KEY` — [Google AI Studio](https://aistudio.google.com/app/apikey)에서 무료 발급
- `SMTP_USER` / `SMTP_PASSWORD` — Gmail 사용 시 [앱 비밀번호](https://myaccount.google.com/apppasswords)
- `NOTIFY_TO` — 알림 받을 이메일 (기본 `jaykim@sooplive.com`)

## 실행

### 1) Streamlit 앱

```powershell
streamlit run app.py
```

브라우저에서 `http://localhost:8501` 자동 오픈.

### 2) 워커 수동 실행

```powershell
python worker.py            # 신규 영상 체크 + 메일
python worker.py --no-mail  # 메일 없이 DB만 업데이트
```

### 3) 자동 스케줄러 등록 (1시간 주기)

PowerShell을 **관리자 권한**으로 실행:

```powershell
cd C:\Users\user\Desktop\youtube-monitor
.\register_scheduler.ps1
```

확인:
```powershell
Get-ScheduledTask -TaskName YouTubeMonitor_Worker
Start-ScheduledTask -TaskName YouTubeMonitor_Worker  # 즉시 실행 테스트
```

해제:
```powershell
Unregister-ScheduledTask -TaskName YouTubeMonitor_Worker -Confirm:$false
```

로그는 `worker.log`에 기록됨.

## 제약사항

- **NotebookLM**은 공식 API가 없어 자동 호출 불가. Gemini로 대체.
- 자막은 `youtube-transcript-api`(비공식)에 의존. 영상에 자막이 없으면 요약 불가 — 추후 Whisper 등 STT 추가 필요.
- 숏폼 판정은 60초 이하 + `#shorts` 태그 휴리스틱. 100% 정확도 보장 X.
- YouTube Data API 일일 쿼터 10,000 units. `playlistItems` 폴링(1 unit/호출) 기준 채널 100개를 시간당 체크해도 안전.
- Gmail SMTP는 일반 비밀번호로는 차단됨 → 반드시 앱 비밀번호 사용.
