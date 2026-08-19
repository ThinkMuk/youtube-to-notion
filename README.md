# YouTube Live → Notion 강의 노트

유튜브 라이브(또는 VOD) 강의를 실시간으로 시청하면서, 슬라이드가 바뀔 때마다 해당 슬라이드 이미지와
강사 발화를 AI로 요약한 한국어 노트를 Notion 페이지에 자동으로 기록해주는 데스크톱 앱입니다.
요약은 기본적으로 Google Gemini(무료 티어)를 사용하며, Anthropic(Claude) API로 전환할 수도 있습니다.

> 🚀 **처음이라면 [GUIDE.md](GUIDE.md)를 먼저 보세요** — Windows 빌드부터 첫 강의 기록,
> PC 이동까지 실사용 순서를 단계별로 안내합니다. 이 README는 기능·설정 레퍼런스입니다.

## 동작 방식

1. 유튜브 URL을 입력하고 **시작** 버튼을 누르면, `yt-dlp`로 스트림 주소를 얻고 `ffmpeg`로 영상/음성을
   캡처합니다. (녹화는 시작 버튼을 누른 시점부터 시작되며, 과거 방송 내용을 소급해서 가져오지 않습니다.)
2. 일정 간격으로 프레임을 캡처해 화면(슬라이드) 변화를 감지합니다.
3. 슬라이드가 바뀌면, 해당 슬라이드가 화면에 떠 있던 구간의 음성을 Whisper로 받아쓰고,
   설정된 AI 백엔드(기본값 Gemini)로 한국어 불릿 요약을 생성한 뒤, 슬라이드 이미지와 함께 Notion 페이지에
   추가합니다.
4. **종료** 버튼을 누르면 마지막 슬라이드를 마무리하고, 전체 강의에 대한 종합 요약을
   Notion 페이지 맨 아래에 추가합니다. 이어서 모든 슬라이드의 Whisper 원문 스크립트를 모은
   하위 페이지("📜 전체 스크립트")를 만들어 요약이 부실하거나 잘못된 경우를 대비한 백업 기록을 남긴 뒤
   세션을 마칩니다.

슬라이드 전환이 감지될 때마다 곧바로 발행하지는 않습니다. 빠른 슬라이드 넘김, 코드 스크롤, 발화 없는
화면 전환처럼 요약할 내용이 부족한 구간은 즉시 올리지 않고 내부적으로 누적(carry-over)해두었다가,
이어지는 화면과 합쳐 이미지 여러 장 + 통합 요약으로 이루어진 하나의 섹션으로 Notion에 기록합니다.
발화가 전혀 없던 구간은 별도 섹션을 만들지 않고 스크린샷만 직전 섹션에 덧붙이며, 이 경우 "요약을
만들지 못했다"는 식의 실패 문구도 발행되지 않습니다.

## 사전 준비물

**Windows exe로 배포받은 최종 사용자**는 별도 설치가 필요 없습니다. Python, ffmpeg 모두
`dist\YoutubeLiveNotion` 폴더 안에 동봉되어 있으며, 혹시 ffmpeg가 빠져 있더라도 첫 실행 시
앱이 자동으로 다운로드합니다 (아래 "ffmpeg 자동 설치" 참고). `config.json`만 채워주면 됩니다.

개발 환경(`python -m yln`으로 소스 실행, 주로 macOS/Linux)에서는 아래가 필요합니다:

- Python 3.11 이상
- [ffmpeg](https://ffmpeg.org/download.html) — PATH에 등록되어 있어야 합니다 (macOS는
  `brew install ffmpeg`). 자동 다운로드 기능은 Windows 전용이므로, macOS/Linux 개발 환경에서는
  시스템 ffmpeg가 필요합니다.
- (권장) [deno](https://deno.com/) — 최신 yt-dlp는 유튜브 포맷 추출에 JS 런타임을 사용합니다.
  없어도 동작하는 경우가 많지만, 일부 화질/포맷이 누락되거나 추출이 실패하면 deno를 설치하세요.
- Notion Integration (연동 앱) 생성 및 페이지 연결
- Google AI Studio(Gemini) API 키 — 기본 요약 백엔드. 무료 티어로 사용 가능하며 신용카드 등록이 필요 없습니다.
  (Anthropic API 키로 전환하고 싶다면 아래 "요약 백엔드 전환" 참고)

### ffmpeg 자동 설치 (Windows)

Windows exe는 다음 순서로 ffmpeg를 찾습니다: ① 실행 파일과 같은 폴더의 `ffmpeg.exe`
(빌드 시 자동 동봉됨) → ② `bin\ffmpeg.exe` → ③ 시스템 PATH → ④ 위 어디에도 없으면
[gyan.dev](https://www.gyan.dev/ffmpeg/builds/) 빌드를 자동으로 다운로드해 `bin\ffmpeg.exe`에
설치합니다 (최초 1회, 약 80MB, 상태 표시줄에 진행 상황이 표시됩니다). 즉, 정상적으로 빌드된
배포본이라면 사용자가 ffmpeg를 신경 쓸 필요가 없습니다.

### Notion 연동 앱 만들기

1. https://www.notion.so/my-integrations 에서 새 Integration을 생성하고, "Internal Integration Token"을 복사해둡니다.
2. 강의 노트를 기록할 상위 페이지(부모 페이지)를 Notion에서 만듭니다.
3. 해당 페이지의 우측 상단 `...` 메뉴 → **연결 추가(Add connections)** 에서 방금 만든 Integration을 연결합니다.
4. 부모 페이지 URL에서 페이지 ID(32자리 하이픈 포함/미포함 문자열)를 확인합니다.

### Gemini API 키 발급 (기본 요약 백엔드)

1. https://aistudio.google.com/apikey 에 구글 계정으로 로그인합니다.
2. **Create API key** 를 눌러 키를 발급받습니다. 신용카드 등록 없이 즉시 무료로 사용할 수 있습니다.
3. 무료 티어 한도는 모델에 따라 다르지만 대략 분당 10회(10 RPM), 일 1,500회(1,500 RPD) 수준입니다.
   슬라이드 전환마다 1회씩 호출되므로 대부분의 강의 시청에는 충분하지만, 한도 초과 시 앱이 자동으로
   재시도(백오프)합니다.
4. 발급받은 키를 `config.json`의 `gemini_api_key`에 넣습니다.

### 요약 백엔드 전환 (선택 사항: Anthropic/Claude 사용하기)

`config.json`의 `summarizer_backend`를 `"anthropic"`으로 바꾸고, `anthropic_api_key`에
https://console.anthropic.com 에서 발급받은 키를 넣으면 Claude로 요약합니다. 이 경우
`gemini_api_key`는 비워둬도 됩니다.

### 요약 백엔드 전환 (선택 사항: Claude 구독으로 사용하기 — API 키 불필요)

Claude Pro/Max 구독이 있다면 API 키 없이 구독 계정으로 요약할 수 있습니다.

1. Claude Code CLI 설치: PowerShell에서 `irm https://claude.ai/install.ps1 | iex`
2. 터미널에서 `claude`를 실행하고 `/login`으로 한 번 로그인 (브라우저 인증)
3. `config.json`의 `summarizer_backend`를 `"claude_code"`로 변경

이 방식은 API 키가 전혀 필요 없고 토큰 단위 과금도 없습니다 (구독 요금제의 사용량 한도 적용).
`claude_code_model`로 모델을 바꿀 수 있습니다 (`"haiku"` 기본값, `"sonnet"` 가능).

앱 화면 우측의 **"Claude 로그인"** 버튼을 누르면 위 1~2단계를 대신해줍니다: CLI가 설치되어
있지 않으면 설치 창(PowerShell)을 열어주고, 설치되어 있으면 새 콘솔 창을 열어 `/login` 흐름으로
로그인할 수 있게 해줍니다. 실습실/공용 PC 등에서 사용을 마친 뒤에는 **"초기화"** 버튼으로 Claude CLI
로그인 정보와 CLI 프로그램 자체, 앱의 `config.json`(Notion 토큰·API 키), 녹화 캐시를 한 번에
정리할 수 있습니다.

## config.json 설정

`config.json.example` 파일을 복사해 `config.json`으로 이름을 바꾸고 값을 채워주세요.

```json
{
  "notion_token": "secret_xxx_your_notion_integration_token",
  "notion_parent_page_id": "your-notion-parent-page-id",
  "summarizer_backend": "gemini",
  "gemini_api_key": "AIzaSy-xxx-your-google-ai-studio-key",
  "gemini_model": "gemini-flash-latest",
  "anthropic_api_key": "",
  "whisper_model": "medium",
  "whisper_device": "auto",
  "frame_interval_sec": 2.0,
  "change_ratio": 0.5,
  "debounce": 3,
  "min_slide_duration_sec": 15.0,
  "min_transcript_chars": 30,
  "substantial_chars": 120,
  "max_merged_segments": 6,
  "max_merged_duration_sec": 300.0,
  "max_images_per_section": 4,
  "audio_flush_timeout_sec": 5.0,
  "idle_flush_sec": 90.0
}
```

- `summarizer_backend`: `"gemini"`(기본값), `"anthropic"`, 또는 `"claude_code"`. API 백엔드는
  해당 API 키만 채우면 되고, `claude_code`는 키 없이 로그인된 Claude Code CLI를 사용합니다.
- `gemini_model`: 기본값 `gemini-flash-latest`. 필요시 다른 Gemini 모델명으로 바꿀 수 있습니다.
- `whisper_model`: `tiny`, `base`, `small`, `medium`, `large-v3` 등. 사양이 낮은 PC라면
  `small`이나 `base`를 권장합니다. `medium`은 정확도가 높지만 첫 실행 시 약 1.5GB를 다운로드합니다.
- `whisper_device`: `auto`, `cpu`, `cuda` 중 선택. `auto`는 GPU(NVIDIA CUDA)를 우선 시도하고,
  CUDA 런타임 라이브러리(cuBLAS/cuDNN)가 설치되어 있지 않으면 자동으로 `cpu`로 전환됩니다.
  GPU가 없거나 CUDA를 설치하지 않은 PC라면 처음부터 `cpu`로 지정해도 됩니다.
- `frame_interval_sec`: 프레임 캡처 간격(초). 값이 작을수록 슬라이드 전환 감지가 빠르지만 CPU 사용량이 늘어납니다.
- `change_ratio`: 슬라이드 전환으로 판단할 화면 변화 비율(0.0~1.0, 기본 0.5). 화면의 이 비율 이상이 바뀌면
  슬라이드 전환으로 판단합니다. 값을 낮추면 더 민감하게 감지합니다.
- `debounce`: 화면 전환을 "확정"하는 데 필요한 연속 안정 프레임 수(기본 3). `frame_interval_sec` × `debounce`초
  동안 새 화면이 안정적으로 유지되어야 전환으로 확정됩니다. 값을 낮추면 전환을 더 빨리 잡아내지만
  커서 움직임이나 전환 애니메이션 같은 순간적인 노이즈에 오탐할 수 있습니다.
- `min_slide_duration_sec`: 병합 그룹의 누적 구간이 이 시간(초, 기본 15) 이상이면 발화 분량이 적어도
  발행 대상으로 간주합니다.
- `min_transcript_chars`: 병합 그룹의 전사 글자 수가 이 값(기본 30) 미만이면 "내용 있음"으로 보지 않고
  다음 화면과 계속 병합합니다.
- `substantial_chars`: 누적 구간의 지속 시간과 무관하게, 전사 글자 수가 이 값(기본 120) 이상이면
  즉시 발행 대상으로 간주합니다.
- `max_merged_segments`: 병합 그룹에 담기는 세그먼트 수가 이 값(기본 6)을 넘으면 내용 유무와 무관하게
  강제로 발행합니다.
- `max_merged_duration_sec`: 병합 그룹의 누적 구간이 이 시간(초, 기본 300)을 넘으면 강제로 발행합니다.
- `max_images_per_section`: 한 섹션에 첨부하는 최대 이미지 장수(기본 4). 유사한 프레임은 먼저 중복
  제거된 뒤, 이 상한 내에서 고르게 샘플링됩니다.
- `audio_flush_timeout_sec`: 세그먼트 처리 시 오디오 캡처가 끝나기를 기다리는 최대 시간(초, 기본 5).
- `idle_flush_sec`: 새 세그먼트가 이 시간(초, 기본 90) 동안 들어오지 않으면, 대기 중인 병합 그룹을
  그대로 강제 발행합니다. 라이브 시청자가 화면 전환 없이 오래 기다리는 상황을 방지합니다.

개발 중(소스 실행)에는 `config.json`을 프로젝트 루트(`yln/` 폴더의 상위 디렉터리)에 두면 됩니다.
Windows exe로 빌드한 뒤에는 실행 파일(`YoutubeLiveNotion.exe`)과 같은 폴더에 두어야 합니다.

## 개발 환경에서 실행하기 (macOS/Linux/Windows 공통)

```bash
python3 -m venv .venv
source .venv/bin/activate  # Windows는 .venv\Scripts\activate
pip install -r requirements.txt
python -m yln
```

## Windows exe 빌드하기

Windows 환경에서:

```bat
build.bat
```

이 스크립트는 가상환경을 만들고, 의존성과 PyInstaller를 설치한 뒤 `youtube-live-notion.spec`으로
빌드를 수행합니다. 이어서 `dist\YoutubeLiveNotion\` 폴더에 ffmpeg.exe가 없으면 자동으로
다운로드해 동봉하고, `config.json.example`도 함께 복사합니다. 즉 빌드가 끝나면
`dist\YoutubeLiveNotion\` 폴더 자체가 실행 파일 + ffmpeg가 모두 갖춰진 배포 가능한 상태가 됩니다.

남은 일은 `config.json.example`을 `config.json`으로 복사해 실제 키 값을 채우는 것뿐입니다.
이후 `YoutubeLiveNotion.exe`를 더블클릭하면 실행됩니다. (ffmpeg 동봉에 실패했거나 다른 버전을
쓰고 싶다면, 앱이 첫 실행 시 자동으로 다운로드를 시도하거나, 위 "ffmpeg 자동 설치" 순서대로
직접 넣어주셔도 됩니다.)

## 포터블 폴더로 다른 PC에서 실행하기

빌드된 `dist\YoutubeLiveNotion` 폴더는 그 자체로 포터블합니다 (build.bat이 ffmpeg.exe까지
자동으로 동봉해줍니다). 아래 구성이 갖춰진 폴더를 통째로 복사하면 새 PC에 설치 과정 없이
바로 실행할 수 있습니다.

```
YoutubeLiveNotion/
  YoutubeLiveNotion.exe
  ffmpeg.exe
  config.json
  models/            <- Whisper 모델 캐시 (첫 실행 후 자동 생성됨)
  ...
```

Whisper 모델은 사용자 홈 폴더가 아니라 실행 파일과 같은 위치의 `models/` 폴더에 다운로드되므로,
한 번 모델을 받아둔 폴더를 복사하면 다른 PC에서는 재다운로드 없이 바로 동작합니다. USB나
네트워크 드라이브로 폴더째 옮겨도 무방합니다.

## 사용법

1. 앱 실행 후 **유튜브 URL** 칸에 라이브 방송 또는 VOD 링크를 입력합니다.
2. **강의 주제 키워드** 칸에 강의 관련 기술 키워드(예: `Java, Spring, JWT`)를 입력하면
   음성 인식 정확도와 요약 품질이 향상됩니다. (선택 사항)
3. **시작** 버튼을 누르면 캡처가 시작되고, 슬라이드가 감지될 때마다 상태 영역에 진행 상황이 표시됩니다.
4. 강의가 끝나면 **종료** 버튼을 눌러 마지막 슬라이드와 전체 요약을 Notion에 기록하고 세션을 마칩니다.
   이때 Notion 페이지 하단에는 강의 전체 요약과 함께, 모든 슬라이드의 Whisper 원문 스크립트를 담은
   "📜 전체 스크립트" 하위 페이지가 함께 생성됩니다. 무료 요약 API가 부실하게 답하거나 실패한 경우에도
   원문 스크립트로 내용을 확인할 수 있는 백업 역할을 합니다.

## 주의사항

- Whisper `medium` 모델은 첫 실행 시 자동으로 다운로드되며 용량이 약 1.5GB입니다. 저사양 PC에서는
  `small` 또는 `base` 모델 사용을 권장합니다.
- 녹화는 항상 **시작 버튼을 누른 시점부터** 시작되며, 라이브 방송의 과거 구간은 가져오지 않습니다.
- 세션 중 개별 슬라이드 처리(음성 인식/요약/업로드)가 실패해도 세션 전체가 중단되지 않고 계속 진행됩니다.
  단, 스트림 연결이나 Notion 페이지 생성 자체가 실패하면 세션이 종료됩니다.
- Gemini 무료 티어는 요청 빈도 제한이 있습니다. 슬라이드 전환이 매우 잦은 강의라면 한도에 걸릴 수
  있으며, 이 경우 앱이 자동으로 재시도합니다. 반복적으로 실패한다면 `frame_interval_sec`을 늘리거나
  `summarizer_backend`를 `"anthropic"`으로 전환하는 것도 방법입니다.
- 전체 스크립트 하위 페이지 기록이 실패해도(예: 네트워크 오류) 이미 기록된 전체 요약은 유지되며,
  세션은 "[오류]" 메시지와 함께 정상적으로 "완료" 상태로 마무리됩니다.
