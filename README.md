# YouTube Live → Notion

유튜브 강의의 슬라이드와 음성을 분석해 **슬라이드 이미지, 한국어 요약, 전체 스크립트**를 Notion에 기록하는 데스크톱 앱입니다.

업로드된 녹화 영상(VOD)과 라이브 URL을 지원하며, 브라우저 재생과 독립적으로 동작합니다. 음성은 로컬 Whisper로 전사하고, 요약은 Codex·Claude Code·Gemini API·Anthropic API 중 선택한 백엔드가 처리합니다.

새 설정은 **Codex / `gpt-5.6-luna` / `low`**를 사용합니다. Windows 배포본에는 Python 실행 환경과 Codex CLI가 포함되며, 빌드 과정에서 ffmpeg와 Deno도 준비합니다.

## 주요 기능

- **슬라이드 감지와 구간 병합** — 화면 변화를 감지하고, 짧거나 발화가 적은 구간은 다음 화면과 묶어 기록합니다.
- **강의 음성 전사** — 한국어 음성을 Whisper로 받아씁니다. 입력한 주제 키워드를 전사와 요약에 참고합니다.
- **Notion 자동 기록** — 강의별 페이지에 이미지와 구간 요약을 추가하고, 종료 시 전체 요약과 스크립트 하위 페이지를 만듭니다.
- **VOD 시작 위치 지정** — 처음부터 또는 지정한 시간부터 분석합니다.
- **요약 AI 전환** — 앱에서 Claude / Codex를 선택하면 즉시 저장되며, 다음 실행과 재빌드 후에도 유지됩니다.
- **Windows 폴더 배포** — 실행 파일과 의존성, 설정, Whisper 모델 캐시를 폴더째 옮길 수 있습니다.

## 빠른 시작 — Windows 배포본

### 1. 배포 폴더 준비

`YoutubeLiveNotion.exe`가 들어 있는 **폴더 전체**를 준비하세요. Codex를 사용하는 배포본은 Python·Node.js·npm을 따로 설치할 필요가 없습니다.

소스만 있다면 [Windows 빌드](#windows-빌드)를 먼저 진행합니다.

### 2. Notion 연결

1. [Notion Integration 설정](https://www.notion.so/my-integrations)에서 연동 앱을 만들고 토큰을 발급받습니다.
2. 강의 노트를 모을 부모 페이지를 만들고, 해당 페이지에 연동 앱을 연결합니다.
3. 부모 페이지 URL에서 페이지 ID를 확인합니다.
4. 실행 파일과 같은 폴더의 `config.json`에 `notion_token`과 `notion_parent_page_id`를 입력합니다.

설정 파일이 없다면 [config.json.example](config.json.example)을 복사해 `config.json`으로 저장하세요. Codex를 사용할 때 필요한 최소 설정은 다음과 같습니다.

```json
{
  "notion_token": "ntn_xxx_your_notion_integration_token",
  "notion_parent_page_id": "your-notion-parent-page-id",
  "summarizer_backend": "codex",
  "codex_model": "gpt-5.6-luna",
  "codex_reasoning_effort": "low"
}
```

나머지 항목은 기본값을 사용합니다. 전체 설정 예시는 `config.json.example`에 있습니다.

### 3. 로그인하고 기록 시작

1. `YoutubeLiveNotion.exe`를 실행합니다.
2. **Codex 로그인**을 눌러 열린 콘솔에서 계정 인증을 완료합니다. 이미 해당 PC에서 로그인했다면 기존 인증을 사용합니다.
3. 요약 AI에서 **Codex**를 선택합니다. 로그인 버튼과 AI 선택은 별개입니다.
4. **유튜브 URL**을 입력하고, 필요하면 **강의 주제 키워드**와 **기록 시작** 위치를 지정합니다.
5. **시작**을 누릅니다. Whisper 모델이 없으면 처음 전사할 때 `models/` 폴더에 다운로드합니다.
6. 기록을 마치려면 **종료**를 누르고, 상태창의 완료 메시지를 기다린 뒤 Notion 결과를 확인합니다.

기록 중에는 요약 AI를 바꿀 수 없습니다. 종료 후에도 남은 전사·요약·업로드가 진행되므로 앱을 바로 닫지 마세요. 다운로드 중 종료 요청은 다운로드를 즉시 취소하지 않을 수 있습니다.

## 입력 영상과 처리 방식

| 입력                    | 처리 방식                                      | 시작 위치                    |
| ----------------------- | ---------------------------------------------- | ---------------------------- |
| 업로드된 녹화 영상(VOD) | 영상을 전체 다운로드한 뒤 프레임과 음성을 분석 | 처음부터 또는 지정 시간부터  |
| 진행 중인 라이브        | 현재 스트림을 수신하면서 분석                  | 앱이 수신을 시작한 이후 구간 |

VOD 시작 시간은 `HH:MM:SS`, `MM:SS`, 정수 초 형식을 받습니다. 지정 시간부터 분석해도 영상은 전체 다운로드하며, Notion에는 원본 영상 기준의 시간이 표시됩니다. 라이브에서는 지정 시간을 무시하고 과거 방송분을 소급해서 가져오지 않습니다.

라이브는 수신이 중단될 수 있으며 자동 재연결·이어받기를 지원하지 않습니다. 전체 강의를 기록할 때는 업로드된 녹화 영상을 사용하세요.

```text
유튜브 URL
  → yt-dlp: 스트림 정보 조회 / VOD 다운로드
  → ffmpeg: 화면 프레임 + 음성 추출
  → 슬라이드 변화 감지
  → faster-whisper: 구간별 음성 전사
  → 짧은 구간 병합 + AI 요약
  → Notion: 이미지와 요약 기록
  → 종료 시 전체 요약 + 스크립트 하위 페이지
```

요약은 **음성 전사문을 기반**으로 합니다. 유튜브 자막이나 슬라이드 이미지의 OCR·시각 분석은 사용하지 않으며, 이미지는 Notion에 참고 자료로 첨부합니다. 따라서 말로 설명하지 않은 슬라이드 내용은 요약에 반영되지 않을 수 있습니다.

## Notion 결과물

설정한 부모 페이지 아래에 강의별 페이지가 생성됩니다.

```text
강의 제목
├─ Slide 1 · 구간 시간
│  ├─ 슬라이드 이미지
│  └─ 한국어 요약
├─ Slide 2 · 구간 시간
│  ├─ 슬라이드 이미지 여러 장
│  └─ 병합한 구간의 요약
├─ 전체 요약
└─ 전체 스크립트 — 강의 제목
   └─ 처리한 구간별 시간과 Whisper 전사문
```

화면 전환마다 별도 섹션이 생기지는 않습니다. 내용이 적은 구간은 합치고, 발화가 없는 구간은 이미지가 앞선 내용 뒤에 추가될 수 있습니다. 앱의 업로드 수는 감지한 화면 전환 수와 다를 수 있습니다.

전체 스크립트는 **이번 실행에서 처리한 구간**의 전사문입니다. 지정 시점 이전이나 수신하지 못한 라이브 구간은 포함하지 않습니다. 전사·요약 오류가 있을 수 있으므로 필요한 내용은 원본 영상과 함께 확인하세요.

## 요약 백엔드

| `summarizer_backend` | 사용 방식                            | 준비할 것               | 관련 설정                               |
| -------------------- | ------------------------------------ | ----------------------- | --------------------------------------- |
| `codex`              | Codex CLI의 기존 인증으로 요약       | Codex CLI 로그인        | `codex_model`, `codex_reasoning_effort` |
| `claude_code`        | Claude Code CLI의 기존 인증으로 요약 | Claude Code 설치·로그인 | `claude_code_model`                     |
| `gemini`             | Gemini API 호출                      | `gemini_api_key`        | `gemini_model`                          |
| `anthropic`          | Anthropic API 호출                   | `anthropic_api_key`     | API 키 설정                             |

- **Codex / Claude Code**: 앱의 토글로 선택합니다. 두 로그인 버튼은 선택한 AI와 관계없이 표시됩니다.
- **Gemini / Anthropic**: 앱을 닫고 `config.json`의 백엔드와 API 키를 수정한 뒤 다시 실행합니다. 키는 각각 [Google AI Studio](https://aistudio.google.com/apikey), [Anthropic Console](https://console.anthropic.com)에서 준비합니다.
- Windows의 **Claude 로그인** 버튼은 CLI가 없으면 설치 과정을 안내하고, 설치되어 있으면 로그인 콘솔을 엽니다.
- CLI 백엔드는 앱 설정에 별도 요약 API 키를 요구하지 않습니다. 모델 접근과 사용량 한도는 로그인한 계정에 따릅니다.

Codex 요약은 임시 작업 폴더에서 비대화형 `exec`로 실행합니다. 전사문을 표준입력으로 전달하고 최종 응답만 읽으며, 개인 설정의 MCP 연결은 불러오지 않습니다. 호출당 제한 시간은 180초이고, 실패 시 최대 두 번 더 시도합니다.

## 설정

소스 실행에서는 **프로젝트 루트**, Windows 배포본에서는 **실행 파일과 같은 폴더**의 `config.json`을 읽습니다. 앱의 AI 토글 외 설정은 앱을 닫은 상태에서 수정하세요.

### 계정과 모델

| 항목                     | 기본값                | 설명                                                                                   |
| ------------------------ | --------------------- | -------------------------------------------------------------------------------------- |
| `notion_token`           | 필수                  | Notion Integration 토큰                                                                |
| `notion_parent_page_id`  | 필수                  | 강의 페이지를 생성할 부모 페이지 ID                                                    |
| `summarizer_backend`     | 예시 파일: `codex`    | `codex`, `claude_code`, `gemini`, `anthropic`                                          |
| `codex_model`            | `gpt-5.6-luna`        | Codex 요약 모델                                                                        |
| `codex_reasoning_effort` | `low`                 | 앱 허용값: `low`, `medium`, `high`, `xhigh`, `max`. 모델에서도 해당 수준을 지원해야 함 |
| `claude_code_model`      | `haiku`               | Claude Code에 전달할 모델명                                                            |
| `gemini_api_key`         | 빈 문자열             | Gemini 선택 시 필수                                                                    |
| `gemini_model`           | `gemini-flash-latest` | Gemini 요약 모델                                                                       |
| `anthropic_api_key`      | 빈 문자열             | Anthropic 선택 시 필수                                                                 |
| `whisper_model`          | `medium`              | 로컬 전사 모델. 예: `tiny`, `base`, `small`, `medium`, `large-v3`                      |
| `whisper_device`         | `auto`                | `auto`, `cpu`, `cuda`                                                                  |

새 예시 설정은 Codex를 선택합니다. 단, 기존 파일을 직접 불러올 때 `summarizer_backend`가 없거나 비어 있으면 Gemini로 해석하므로 백엔드를 명시하세요.

Whisper는 로컬 모델 캐시를 먼저 사용하고, 캐시가 없을 때 다운로드합니다. `auto`는 장치를 자동 선택하며, CUDA 라이브러리 오류가 감지되면 CPU로 전환합니다. CPU 처리 속도가 부족하면 더 작은 모델로 조정할 수 있습니다.

### 캡처와 영상

| 항목                      | 기본값 | 설명                                                                         |
| ------------------------- | ------ | ---------------------------------------------------------------------------- |
| `frame_interval_sec`      | `2.0`  | 프레임 추출 간격(초)                                                         |
| `change_ratio`            | `0.5`  | 화면 변화 비율 기준. 낮출수록 민감하며, 실제 적용 범위는 `0.05`~`0.95`       |
| `debounce`                | `3`    | 새 화면이 안정적으로 유지되어야 하는 연속 프레임 수                          |
| `audio_flush_timeout_sec` | `5.0`  | 구간 처리 전 오디오가 기록되기를 기다리는 최대 시간(초)                      |
| `stall_timeout_sec`       | `60.0` | 라이브 오디오 수신이 멈췄다고 판단하는 시간(초). `0`이면 감시 비활성화       |
| `vod_max_height`          | `1080` | VOD 다운로드 요청 시 최대 영상 높이(px). 실제 화질은 제공 포맷에 따라 달라짐 |

### 구간 병합과 발행

| 항목                      | 기본값  | 설명                                                                      |
| ------------------------- | ------- | ------------------------------------------------------------------------- |
| `min_transcript_chars`    | `30`    | 내용이 있는 구간으로 판단할 최소 전사 글자 수                             |
| `min_slide_duration_sec`  | `15.0`  | 최소 전사 분량을 충족한 그룹을 발행 대상으로 삼는 누적 길이(초)           |
| `substantial_chars`       | `120`   | 최소 전사 분량을 충족하면 구간 길이와 관계없이 발행 대상으로 삼는 글자 수 |
| `max_merged_segments`     | `6`     | 병합 구간 수가 이 값에 도달하면 강제 처리                                 |
| `max_merged_duration_sec` | `300.0` | 병합 구간 길이가 이 값에 도달하면 강제 처리(초)                           |
| `max_images_per_section`  | `4`     | 한 섹션의 최대 이미지 수. 유사 이미지 제거 후 고르게 선택                 |
| `idle_flush_sec`          | `90.0`  | 새 구간이 들어오지 않을 때 대기 중인 그룹을 강제 처리하기까지의 시간(초)  |

기본 설정에서는 **전사 30자 이상이면서 구간 15초 이상**, 또는 **전사 120자 이상**이면 요약을 시도합니다. 요약이 유효하지 않으면 다음 구간과 더 합칠 수 있고, 강제 처리 시에도 발화가 없으면 이미지 위주로 기록합니다.

## Windows 빌드

빌드하는 PC에는 **Python 3.11 이상**이 필요합니다. 실행 중인 앱을 닫고 Windows 프로젝트 폴더에서 실행하세요.

```powershell
.\build.bat
```

빌드 스크립트는 가상환경과 의존성을 준비하고 PyInstaller로 앱을 묶습니다.

- `requirements.txt`에 고정한 **Codex CLI 0.154.0**과 보조 파일을 동봉하고, 배포 폴더의 실행 파일로 `--version`을 검사합니다.
- ffmpeg와 Deno를 다운로드해 배포 폴더에 준비합니다.
- 기존 설정과 선택한 요약 AI를 복원하고, 없는 Codex 설정 항목만 기본값으로 채웁니다.
- 기존 Whisper 모델 캐시는 빌드 전에 옮겨두었다가 성공 후 복원합니다.

설정 보존은 `config.build-pending.json` → 기존 배포 폴더의 `config.json` → 프로젝트 루트의 `config.json` → `config.json.example` 순서입니다. 실패한 빌드의 대기 파일이 있으면 그것을 우선 복구하며, 복원한 원본 설정은 프로젝트 루트의 `config.build-backup.json`에 남습니다.

### 다른 Windows PC로 옮기기

`dist\YoutubeLiveNotion` 폴더 전체를 복사합니다.

```text
YoutubeLiveNotion/
├─ YoutubeLiveNotion.exe
├─ config.json
├─ config.json.example
├─ ffmpeg.exe
├─ deno.exe
├─ _internal/
│  └─ codex_cli_bin/        # Codex 실행 파일과 보조 파일
└─ models/                 # Whisper 최초 사용 후 생성
```

`models/`까지 옮기면 받은 모델 캐시를 재사용합니다. Codex 인증은 배포 폴더에 포함되지 않으므로 새 PC에서는 로그인해야 합니다. Claude Code를 선택했다면 그 PC에도 CLI 설치와 로그인이 필요합니다.

## 소스에서 실행

Python 3.11 이상과 Tkinter를 사용할 수 있는 환경이 필요합니다. macOS/Linux에서는 ffmpeg를 PATH에 준비하세요. Deno가 있으면 VOD의 고화질 포맷 추출에 사용할 수 있습니다.

이미 `config.json`이 있다면 아래 명령의 복사 단계를 건너뛰어 기존 설정을 유지하세요.

**macOS / Linux**

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp config.json.example config.json
# config.json의 Notion 값을 입력한 뒤 실행
python -m yln
```

**Windows PowerShell**

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item config.json.example config.json
# config.json의 Notion 값을 입력한 뒤 실행
python -m yln
```

Windows에서는 의존성 설치 시 Codex 런타임도 설치됩니다. macOS/Linux에서 Codex를 사용하려면 별도로 CLI를 설치하고 터미널에서 `codex login`을 완료해야 합니다. Claude Code도 설치한 CLI의 인증을 사용합니다.

테스트는 의존성을 설치한 가상환경에서 실행합니다.

```bash
python -m pip install pytest
python -m pytest -q
```

## 문제 해결과 기록 범위

| 증상                          | 확인할 내용                                                             |
| ----------------------------- | ----------------------------------------------------------------------- |
| 설정 파일을 찾지 못함         | 소스는 프로젝트 루트, 배포본은 exe 옆에 `config.json`이 있는지 확인     |
| Notion 페이지 생성 실패       | 토큰, 부모 페이지 ID, 해당 페이지의 Integration 연결 확인               |
| Codex 인증 오류               | **Codex 로그인**에서 인증 완료 후 다시 시작                             |
| 배포본의 Codex 실행 파일 오류 | 전역 CLI 설치 대신 소스에서 `build.bat`으로 다시 빌드                   |
| VOD 다운로드 실패·낮은 화질   | 영상 접근 가능 여부와 Deno 설치·동봉 여부 확인                          |
| 라이브가 중간에 멈춤          | 상태창 오류 확인. 자동 재연결은 없으며 업로드된 녹화 영상으로 다시 기록 |
| 첫 전사가 오래 걸림           | 상태창의 모델 다운로드·로딩 확인. 이후에는 `models/` 캐시 사용          |
| 슬라이드가 너무 잘게 나뉨     | `debounce`나 `min_slide_duration_sec` 조정                              |
| 슬라이드 전환을 놓침          | `change_ratio`나 `debounce`를 낮춰 감지 조건 조정                       |
| API 요청 한도 오류            | 해당 서비스의 사용량 확인. 자동 재시도 후에도 실패하면 상태창 오류 확인 |

Windows에서 ffmpeg는 앱 폴더 → `bin/` → PATH 순서로 찾고, 없으면 자동 다운로드를 시도합니다. macOS/Linux에서는 직접 설치해야 합니다.

개별 구간 처리에 실패해도 다음 구간은 계속 처리하지만, 실패한 업로드를 나중에 자동 복구하지는 않습니다. 전사문은 종료 전까지 메모리에 보관하며 별도 로컬 텍스트 파일로 저장하지 않습니다. 최종 요약 생성·기록이 실패하면 스크립트 하위 페이지도 생성되지 않을 수 있고, 스크립트 업로드 실패 후에도 완료 메시지가 표시될 수 있으므로 **상태창의 오류와 Notion 결과를 함께 확인하세요.**

### 초기화와 로그아웃

앱의 **초기화**는 Claude 로그인 정보·CLI 프로그램·`~/.claude` 전체, 앱의 `config.json`, 녹화 임시 파일을 삭제하는 기능입니다. 다른 Claude 작업의 설정과 대화 기록도 삭제 대상입니다.

Codex 로그인, Whisper 모델 캐시, 빌드 설정 백업은 초기화 대상에 포함되지 않습니다. Windows 배포본에서 Codex를 로그아웃하려면 배포 폴더의 PowerShell에서 실행하세요.

```powershell
.\_internal\codex_cli_bin\bin\codex.exe logout
```
