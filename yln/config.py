"""Configuration loading for the YouTube Live -> Notion app."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

_VALID_BACKENDS = ("gemini", "anthropic", "claude_code")


class ConfigError(Exception):
    """Raised when config.json is missing or invalid. Message is Korean-facing."""


@dataclass
class Config:
    notion_token: str
    notion_parent_page_id: str
    summarizer_backend: str = "gemini"
    anthropic_api_key: str = ""
    gemini_api_key: str = ""
    gemini_model: str = "gemini-flash-latest"
    claude_code_model: str = "haiku"
    whisper_model: str = "medium"
    whisper_device: str = "auto"
    frame_interval_sec: float = 2.0
    change_ratio: float = 0.5
    debounce: int = 3
    min_slide_duration_sec: float = 15.0
    min_transcript_chars: int = 30
    substantial_chars: int = 120
    max_merged_segments: int = 6
    max_merged_duration_sec: float = 300.0
    max_images_per_section: int = 4
    audio_flush_timeout_sec: float = 5.0
    idle_flush_sec: float = 90.0
    # Live capture is declared stalled when audio stops growing for this long
    # while ffmpeg is still alive (YouTube cutting off HLS segments leaves
    # ffmpeg retrying forever with no EOF). 0 disables the watchdog.
    stall_timeout_sec: float = 60.0
    # Upload (VOD) mode downloads the video before processing; this caps the
    # video height requested from YouTube (1080 keeps slide text readable).
    vod_max_height: int = 1080


def app_dir() -> Path:
    """Directory the app lives in: the frozen exe's directory when packaged
    (PyInstaller), otherwise the project root (parent of the yln/ package).
    Used for both config.json lookup and the portable Whisper model cache.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def load_config() -> Config:
    config_path = app_dir() / "config.json"

    if not config_path.exists():
        raise ConfigError(
            f"config.json 파일을 찾을 수 없습니다.\n"
            f"다음 경로에 config.json 파일을 만들어 주세요:\n{config_path}\n"
            f"(config.json.example 파일을 참고하세요.)"
        )

    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError(f"config.json 파일의 형식이 올바르지 않습니다: {e}") from e

    required_keys = ["notion_token", "notion_parent_page_id"]
    missing = [k for k in required_keys if not raw.get(k)]
    if missing:
        raise ConfigError(
            "config.json에 다음 필수 항목이 비어 있거나 없습니다: " + ", ".join(missing)
        )

    # Accept "claude-code" / "claude code" spellings for the claude_code backend.
    backend = str(raw.get("summarizer_backend") or "gemini").strip().lower().replace("-", "_").replace(" ", "_")
    if backend not in _VALID_BACKENDS:
        raise ConfigError(
            f"summarizer_backend 값이 올바르지 않습니다: '{backend}' "
            f"(가능한 값: {', '.join(_VALID_BACKENDS)})"
        )

    if backend == "gemini" and not raw.get("gemini_api_key"):
        raise ConfigError(
            "summarizer_backend가 'gemini'로 설정되어 있지만 config.json의 "
            "gemini_api_key가 비어 있습니다. Google AI Studio에서 발급받은 키를 입력해주세요."
        )
    if backend == "anthropic" and not raw.get("anthropic_api_key"):
        raise ConfigError(
            "summarizer_backend가 'anthropic'으로 설정되어 있지만 config.json의 "
            "anthropic_api_key가 비어 있습니다. Anthropic Console에서 발급받은 키를 입력해주세요."
        )

    return Config(
        notion_token=raw["notion_token"],
        notion_parent_page_id=raw["notion_parent_page_id"],
        summarizer_backend=backend,
        anthropic_api_key=raw.get("anthropic_api_key", ""),
        gemini_api_key=raw.get("gemini_api_key", ""),
        gemini_model=raw.get("gemini_model", "gemini-flash-latest"),
        claude_code_model=raw.get("claude_code_model", "haiku"),
        whisper_model=raw.get("whisper_model", "medium"),
        whisper_device=raw.get("whisper_device", "auto"),
        frame_interval_sec=float(raw.get("frame_interval_sec", 2.0)),
        change_ratio=min(0.95, max(0.05, float(raw.get("change_ratio", 0.5)))),
        debounce=max(1, int(raw.get("debounce", 3))),
        min_slide_duration_sec=max(0.0, float(raw.get("min_slide_duration_sec", 15.0))),
        min_transcript_chars=max(0, int(raw.get("min_transcript_chars", 30))),
        substantial_chars=max(0, int(raw.get("substantial_chars", 120))),
        max_merged_segments=max(1, int(raw.get("max_merged_segments", 6))),
        max_merged_duration_sec=max(0.0, float(raw.get("max_merged_duration_sec", 300.0))),
        max_images_per_section=max(1, int(raw.get("max_images_per_section", 4))),
        audio_flush_timeout_sec=max(0.0, float(raw.get("audio_flush_timeout_sec", 5.0))),
        idle_flush_sec=max(0.0, float(raw.get("idle_flush_sec", 90.0))),
        stall_timeout_sec=max(0.0, float(raw.get("stall_timeout_sec", 60.0))),
        vod_max_height=max(144, int(raw.get("vod_max_height", 1080))),
    )
