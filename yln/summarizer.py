"""Korean lecture-transcript summarization via a pluggable backend
(Gemini API, Claude API, or the local Claude Code CLI).

`build_summarizer(config)` selects the backend from `config.summarizer_backend`.
All backends share the same prompts and parsing logic; only `_call` differs.
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from typing import TYPE_CHECKING, List, Optional

import requests

from yln.claude_cli import find_claude_cli

if TYPE_CHECKING:
    from yln.config import Config

ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
GEMINI_DEFAULT_MODEL = "gemini-flash-latest"

_RETRY_DELAYS = (2, 8)  # seconds, exponential backoff between the 3 attempts

_SLIDE_SYSTEM_PROMPT = """\
You are a note-taking assistant for a Korean-language technical lecture recording.
You receive a raw speech-to-text transcript covering the time one screen (a \
presentation slide or code) was displayed. The transcript may contain phonetic \
mis-transcriptions of technical terms (Whisper mishears English/technical \
loanwords), and hallucinated filler sentences from silent stretches.

Your task:
1. Condense the transcript into 1 to 6 concise Korean bullet points capturing the \
key content the lecturer spoke about. One bullet is enough when there is little \
content — never pad the list or restate the same point in different words.
If the input contains multiple [HH:MM:SS] time markers, it merges several screens: \
cover the key point of each screen without dropping any.
2. Preserve concrete details — numbers, function/method/option names, commands, \
terminology — instead of generalizing them (e.g. prefer "결측치를 dropna()로 제거" \
over "결측치를 처리").
3. Exclude greetings, small talk, class logistics (breaks, playback speed, mic \
checks), and meaningless repeated sentences.
4. Correct phonetically mis-transcribed technical terms to their standard notation \
when it is clearly a technical term (e.g. '제이더블유티' -> 'JWT', '도커' -> 'Docker').
5. Output ONLY the bullet lines, one per line, in Korean. Do not number them, do \
not add any preamble, heading, or explanation. Do not use markdown bullet markers \
like '-' or '*'.
6. If there is no substantive lecture content to summarize (silence, small talk \
only, suspected hallucinated text), output exactly this single line and nothing \
else: NO_CONTENT
Never output an apology or a sentence like "요약할 내용이 없습니다".
"""

_LECTURE_SYSTEM_PROMPT = """\
You are a note-taking assistant that writes structured Korean lecture summaries.
You receive the bullet-point summaries collected from every slide of a technical \
lecture, in order, along with the lecture title.

Synthesize them into a single structured Korean summary with exactly these three \
plain-text section headers (no markdown '#'):

주제
핵심 개념
정리

Under each header, write clear Korean prose or bullet-style lines summarizing the \
lecture. Do not repeat the headers elsewhere. Do not add any other sections.
"""

_LECTURE_EMPTY_MESSAGE = "(요약할 음성 내용이 없습니다)"
_LECTURE_FAILURE_MESSAGE = "(전체 요약 생성에 실패했습니다 — 슬라이드 요약을 참고해 주세요)"


class SummarizerError(Exception):
    """Raised on unrecoverable summarizer backend failures. Message is Korean-facing."""


_NO_CONTENT_SENTINEL = "NO_CONTENT"

# sentinel 판정은 줄 단위 정확 매칭: 어느 한 줄이 (불릿 기호/인용부호/구두점
# 장식을 제외하면) NO_CONTENT뿐일 때만 판정으로 본다. 부분 문자열 매칭은
# "HTTP 204 NO_CONTENT 응답은 …" 같은 정당한 기술 불릿을 오탐하고, 길이 가드
# (len<100)는 sentinel 뒤에 판정 사유 문단을 덧붙이는 CLI 백엔드 응답을 미탐한다.
_NO_CONTENT_LINE_RE = re.compile(
    r"^[\s>\"'`*\-•·([]*no[\s_-]?content[\s.!:\])\"'`]*$", re.IGNORECASE
)

# 모델이 sentinel 대신 자연어로 "요약할 내용 없음"을 말하는 응답 — 거부(재시도
# 대상)가 아니라 판정이므로 NO_CONTENT와 동일하게 취급한다. 재시도해도 같은
# 결과만 나온다. 시스템 프롬프트가 금지하는 표현이지만 CLI 백엔드의 지시
# 불이행률(실측 19.7%)을 고려하면 방어가 필요하다.
_NO_CONTENT_PHRASES = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"요약할\s*(?:수\s*있는\s*|만한\s*)?[\w가-힣]*\s*내용이\s*(?:없|존재하지\s*않)",
        r"추출할\s*수\s*있는\s*[\w가-힣]*\s*(?:내용이|정보가)\s*(?:없|존재하지\s*않)",
        r"no\s+(?:\w+\s+)?content\s+to\s+summari[sz]e",
        r"nothing\s+to\s+summari[sz]e",
    )
]

# 응답 전체에 한글이 하나도 없으면 거부/환각 응답으로 간주한다 (줄 단위 아님 —
# `SELECT * FROM ...` 같은 정당한 코드 줄이 섞여 있어도 전체 응답 기준이면 오탐이 없다).
_HANGUL_RE = re.compile(r"[가-힣]")

# LLM이 '• ' 같은 불릿 기호를 붙이면 Notion bulleted_list_item과 겹쳐 '• •' 이중
# 불릿이 되는 실측 버그가 있어, 라인 선두의 불릿/번호 접두사를 제거한다.
_BULLET_PREFIX_RE = re.compile(r"^\s*(?:[-*•·▪◦–—]|\d+[.)]|[①-⑳])+\s*")

_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ESCAPE_REPLACEMENTS = (
    (r"\*", "*"),
    (r"\_", "_"),
    (r"\`", "`"),
    (r"\[", "["),
    (r"\]", "]"),
)

# 아래 두 티어의 패턴은 모두 실제 운영에서 Notion에 발행됐던 거부/사과 문구를
# 근거로 만들었다 (76섹션 중 15건, 19.7%). 티어1은 거부 특이도가 높아 길이 400자
# 미만에서만, 티어2는 일반적인 패턴이라 길이 150자 미만에서만 적용한다 — 예를
# 들어 '붙여넣' 같은 단어는 정상 강의 요약("코드를 붙여넣어 실행")에도 나올 수
# 있으므로 길이 가드 없이는 오탐이 발생한다.
_REJECTION_PATTERNS_TIER1 = [
    re.compile(p)
    for p in (
        r"스크립트를? *(제공|붙여|입력)",
        r"붙여넣어 *주",
        r"제공해 *주(세요|시면)",
        r"입력해 *주세요",
        r"전달해 *주세요",
        r"텍스트가 보이지 않",
        r"받지 못했습니다",
        r"전사본[은이가]? *(매우 *불완전|강의 *내용이 *없)",
        r"transcript",
        r"트랜스크립트",
        r"어시스턴트",
        r"안녕하세요!",
    )
]
_REJECTION_PATTERNS_TIER2 = [
    re.compile(p)
    for p in (
        r"^\s*(죄송|미안)",
        r"강의 주제 키워드만",
        r"내용을 추출하기 어렵",
        r"확인할 수 없습니다",
        r"찾을 수 없습니다",
    )
]


def _is_no_content_sentinel(t: str) -> bool:
    # 어느 한 줄이 통째로 sentinel이면 판정으로 본다: CLI 백엔드가 sentinel 뒤에
    # 판정 사유 문단을 덧붙여도(실측 100자 이상 가능) 잡히고, 정당한 불릿 속의
    # NO_CONTENT 부분 문자열("HTTP 204 NO_CONTENT 응답은 …")은 잡지 않는다.
    return any(_NO_CONTENT_LINE_RE.match(line) for line in t.splitlines())


def _is_no_content_phrase(t: str) -> bool:
    # 길이 가드는 tier1과 동일한 이유: 정상 요약이 길어지면 "…내용이 없" 같은
    # 부분 문구가 우연히 포함될 수 있다. 판정 응답은 항상 짧다.
    return len(t) < 400 and any(p.search(t) for p in _NO_CONTENT_PHRASES)


def _is_rejection(t: str) -> bool:
    if len(t) < 400 and any(p.search(t) for p in _REJECTION_PATTERNS_TIER1):
        return True
    if len(t) < 150 and any(p.search(t) for p in _REJECTION_PATTERNS_TIER2):
        return True
    return False


def _clean_line(line: str) -> str:
    for escaped, plain in _ESCAPE_REPLACEMENTS:
        line = line.replace(escaped, plain)
    line = _BOLD_RE.sub(r"\1", line)
    return line.strip()


def _postprocess_bullets(text: str) -> Optional[List[str]]:
    """Validate and clean a slide-summary LLM response into bullet lines.

    Return contract (three states):
    - non-empty list: 정상 요약 불릿.
    - []: 모델이 "요약할 내용 없음"을 판정 (NO_CONTENT sentinel 또는 자연어
      판정 문구) — 실패가 아니므로 재시도 예산을 태우지 않고 carry-over를
      지속하다가, 강제 발행 시점에는 placeholder 대신 무발화 그룹
      (스크린샷-only)으로 처리해야 한다.
    - None: 무효 응답 (빈 응답, 한글 없음, 거부/사과) — 재시도(carry-over) 대상.
    """
    t = text.strip()
    if not t:
        return None
    if _is_no_content_sentinel(t):
        return []
    # 자연어 판정 문구는 한글 부재 검사보다 먼저 봐야 영어 판정
    # ("No content to summarize.")이 무효 응답으로 오분류되지 않는다.
    if _is_no_content_phrase(t):
        return []
    if not _HANGUL_RE.search(t):
        return None
    if _is_rejection(t):
        return None

    lines: List[str] = []
    seen = set()
    for raw_line in t.splitlines():
        line = _BULLET_PREFIX_RE.sub("", raw_line)
        line = _clean_line(line)
        if not line or line in seen:
            continue
        seen.add(line)
        lines.append(line)
        if len(lines) >= 6:
            break

    return lines if lines else None


def _digest(slide_summaries: List[List[str]]) -> str:
    parts = []
    for i, bullets in enumerate(slide_summaries, start=1):
        parts.append(f"[Slide {i}]")
        parts.extend(f"- {b}" for b in bullets)
    return "\n".join(parts)


class Summarizer:
    """Backend-agnostic summarizer interface. Subclasses implement `_call`."""

    def _call(self, system: str, user_content: str, max_tokens: int) -> str:
        raise NotImplementedError

    def summarize_slide(self, transcript: str, topic_keywords: str = "") -> Optional[List[str]]:
        # 반환 규약은 _postprocess_bullets와 동일: 불릿 리스트 = 정상 요약,
        # [] = 요약할 내용 없음 판정(예산 미소모 carry-over, 강제 발행 시 무발화 처리),
        # None = 무효 응답(carry-over 재시도).
        if not transcript or not transcript.strip():
            return []

        user_content = transcript.strip()
        if topic_keywords:
            user_content = f"[강의 주제 키워드: {topic_keywords}]\n\n{user_content}"

        # Generous cap: Gemini "thinking" Flash models spend output tokens on
        # internal reasoning before the visible answer; a tight cap can exhaust
        # the budget and return no parts at all.
        text = self._call(_SLIDE_SYSTEM_PROMPT, user_content, max_tokens=4096)
        return _postprocess_bullets(text)

    def summarize_lecture(self, slide_summaries: List[List[str]], title: str) -> str:
        if not slide_summaries:
            return _LECTURE_EMPTY_MESSAGE

        user_content = f"강의 제목: {title}\n\n{_digest(slide_summaries)}"
        text = self._call(_LECTURE_SYSTEM_PROMPT, user_content, max_tokens=8192)
        t = text.strip()
        if not t or _is_no_content_sentinel(t) or _is_no_content_phrase(t) or _is_rejection(t):
            return _LECTURE_FAILURE_MESSAGE
        return t


class AnthropicSummarizer(Summarizer):
    """Claude API backend. The `anthropic` client is built lazily on first use so
    constructing this object (or importing this module) never requires a valid key."""

    def __init__(self, api_key: str, model: str = ANTHROPIC_MODEL):
        self.api_key = api_key
        self.model = model
        self._client = None

    def _client_instance(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic(api_key=self.api_key)
        return self._client

    def _call(self, system: str, user_content: str, max_tokens: int) -> str:
        last_exc: Optional[Exception] = None
        for attempt in range(3):
            try:
                response = self._client_instance().messages.create(
                    model=self.model,
                    max_tokens=max_tokens,
                    system=system,
                    messages=[{"role": "user", "content": user_content}],
                )
                return "".join(block.text for block in response.content if block.type == "text")
            except Exception as e:  # noqa: BLE001 - broad by design, re-raised after retries
                last_exc = e
                if attempt < len(_RETRY_DELAYS):
                    time.sleep(_RETRY_DELAYS[attempt])
        raise SummarizerError(f"Claude API 호출 실패: {last_exc}") from last_exc


class GeminiSummarizer(Summarizer):
    """Google AI Studio (Gemini) REST backend. Plain `requests`, no SDK dependency."""

    API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

    def __init__(self, api_key: str, model: str = GEMINI_DEFAULT_MODEL):
        self.api_key = api_key
        self.model = model

    def _call(self, system: str, user_content: str, max_tokens: int) -> str:
        url = f"{self.API_BASE}/{self.model}:generateContent"
        headers = {
            "x-goog-api-key": self.api_key,
            "Content-Type": "application/json",
        }
        payload = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user_content}]}],
            "generationConfig": {"maxOutputTokens": max_tokens},
        }

        last_exc: Optional[SummarizerError] = None
        for attempt in range(3):
            try:
                resp = requests.post(url, headers=headers, json=payload, timeout=60)
            except requests.RequestException as e:
                last_exc = SummarizerError(f"Gemini API 요청 실패: {e}")
                if attempt < len(_RETRY_DELAYS):
                    time.sleep(_RETRY_DELAYS[attempt])
                    continue
                raise last_exc

            if resp.status_code >= 400:
                retryable = resp.status_code == 429 or resp.status_code >= 500
                last_exc = SummarizerError(f"Gemini API 오류 {resp.status_code}: {resp.text}")
                if retryable and attempt < len(_RETRY_DELAYS):
                    retry_after = resp.headers.get("Retry-After")
                    wait = float(retry_after) if retry_after else _RETRY_DELAYS[attempt]
                    time.sleep(wait)
                    continue
                raise last_exc

            data = resp.json()
            candidates = data.get("candidates") or []
            if not candidates:
                feedback = data.get("promptFeedback")
                raise SummarizerError(f"Gemini 응답에 candidates가 없습니다 (안전 차단 가능성): {feedback}")

            content = candidates[0].get("content") or {}
            parts = content.get("parts") or []
            # Thinking models may include internal-reasoning parts flagged
            # "thought": true — only visible answer parts count.
            text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
            if not text:
                finish = candidates[0].get("finishReason")
                raise SummarizerError(
                    f"Gemini 응답에 본문 텍스트가 없습니다 (finishReason={finish}). "
                    "maxOutputTokens 부족 또는 안전 차단 가능성이 있습니다."
                )
            return text

        assert last_exc is not None
        raise last_exc


class ClaudeCodeSummarizer(Summarizer):
    """Claude Code CLI backend: shells out to the locally installed `claude` CLI
    in print mode (`claude -p`). Requests run on the user's logged-in Claude
    subscription, so no API key is needed and no per-token API billing occurs.
    Requires the CLI to be installed and logged in once (`claude` in a terminal).
    """

    def __init__(self, model: str = "haiku"):
        self.model = model
        self._cli: Optional[str] = None
        # Older CLI builds without `--system-prompt` fall back to concatenating
        # the system prompt into the stdin payload (weaker instruction-following).
        self._legacy_prompt = False

    def _cli_path(self) -> str:
        if self._cli is None:
            path = find_claude_cli()
            if not path:
                raise SummarizerError(
                    "claude CLI를 찾을 수 없습니다. Claude Code CLI를 설치한 뒤 "
                    "터미널에서 `claude`를 한 번 실행해 로그인해 주세요. "
                    "(PowerShell 설치: irm https://claude.ai/install.ps1 | iex)"
                )
            self._cli = path
        return self._cli

    def _call(self, system: str, user_content: str, max_tokens: int) -> str:
        # max_tokens is not applicable to the CLI and is ignored.
        # Keep the console=False exe from flashing a terminal window per call.
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

        last_exc: Optional[SummarizerError] = None
        for attempt in range(3):
            if self._legacy_prompt:
                cmd = [self._cli_path(), "-p", "--model", self.model, "--output-format", "text"]
                stdin_text = f"{system}\n\n---\n\n{user_content}"
            else:
                # A real system prompt keeps the note-taking instructions separate
                # from the transcript, instead of mixing both into one user message.
                cmd = [
                    self._cli_path(), "-p", "--system-prompt", system,
                    "--model", self.model, "--output-format", "text",
                ]
                stdin_text = user_content
            try:
                proc = subprocess.run(
                    cmd,
                    input=stdin_text,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=180,
                    creationflags=creationflags,
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    return proc.stdout.strip()
                if not self._legacy_prompt:
                    err_text = (proc.stderr or proc.stdout or "").lower()
                    if "system-prompt" in err_text or "unknown option" in err_text or "unrecognized" in err_text:
                        self._legacy_prompt = True
                        continue
                detail = (proc.stderr or proc.stdout or "").strip()[:500]
                last_exc = SummarizerError(
                    f"claude CLI 호출 실패 (exit {proc.returncode}): {detail} "
                    "(로그인이 안 되어 있다면 터미널에서 `claude`를 실행해 로그인해 주세요.)"
                )
            except subprocess.TimeoutExpired:
                last_exc = SummarizerError("claude CLI 호출이 시간 초과되었습니다 (180초).")
            except OSError as e:
                last_exc = SummarizerError(f"claude CLI 실행 실패: {e}")
            if attempt < len(_RETRY_DELAYS):
                time.sleep(_RETRY_DELAYS[attempt])
        assert last_exc is not None
        raise last_exc


def build_summarizer(config: "Config") -> Summarizer:
    backend = (config.summarizer_backend or "gemini").strip().lower()
    if backend == "gemini":
        return GeminiSummarizer(api_key=config.gemini_api_key, model=config.gemini_model)
    if backend == "anthropic":
        return AnthropicSummarizer(api_key=config.anthropic_api_key)
    if backend == "claude_code":
        return ClaudeCodeSummarizer(model=config.claude_code_model)
    raise SummarizerError(f"알 수 없는 요약 백엔드입니다: {backend}")
