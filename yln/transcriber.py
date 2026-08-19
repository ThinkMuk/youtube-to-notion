"""Speech-to-text via faster-whisper. faster_whisper is imported lazily so that
importing this module does not require the (heavy) dependency to be loaded
until transcription is actually needed.
"""

from __future__ import annotations

import difflib
import os
import re
import sys
import threading
from pathlib import Path
from typing import List, Optional

import numpy as np

from yln.config import app_dir

_CUDA_ERROR_MARKERS = ("cublas", "cudnn", "cuda", "zlibwapi")

_cuda_dlls_registered = False

# Loaded models, shared across Transcriber instances: every session constructs
# its own Transcriber, and without this cache each session start pays the full
# model load again in the same process. Keyed by the *requested* (model_size,
# device) — after a CUDA→CPU fallback the CPU model is stored under the
# original key, so later sessions skip the doomed CUDA attempt too. The lock
# makes an overlapping load (old session finalizing while a new one starts)
# wait for the first load instead of holding two copies of the model in RAM.
_MODEL_CACHE: dict = {}
_MODEL_LOCK = threading.Lock()

# faster-whisper hallucinates canned outro/subscribe-and-like phrases (and
# occasionally news-broadcast bylines) during low-volume/silent stretches,
# especially with the Korean model. Strip them so they don't masquerade as
# real content past the downstream "has content" length gate.
_HALLUCINATION_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"구독\s*(과|하)?\s*좋아요",
        r"좋아요\s*(와|과)?\s*구독",
        r"시청해\s*주셔서\s*감사합니다",
        r"시청\s*감사합니다",
        r"다음\s*영상에서\s*만나요",
        r"영상이\s*도움이\s*되셨다면",
        r"알림\s*설정(까지)?",
        r"MBC\s*뉴스",
        r"KBS\s*뉴스",
    )
]

# Sentence boundary: right after '.', '?', '!' (kept with the preceding
# sentence) or a run of newlines.
_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.?!])\s+|\n+")

# Whisper echoes its decoder-prompt text back as "transcription" on silent /
# low-volume stretches (observed verbatim per silent segment with the old
# sentence-form initial_prompt: "이 강의는 AI, ML ... 관련 기술 강의입니다").
# The prompt text is a string this app builds (now the topic-keyword hotwords),
# so any transcript sentence that is near-identical to it after normalization
# is a hallucination, not speech. 0.85 tolerates spacing/punctuation drift in
# the echo while staying far above real lecture sentences that merely mention
# the same keywords (those measure ~0.5 or below).
_PROMPT_ECHO_SIMILARITY = 0.85

_ECHO_NORMALIZE_RE = re.compile(r"[\W_]+")


def _normalize_for_echo(text: str) -> str:
    return _ECHO_NORMALIZE_RE.sub("", text).lower()


def _drop_prompt_echoes(text: str, prompt_text: Optional[str]) -> str:
    if not text or not prompt_text:
        return text
    prompt_norm = _normalize_for_echo(prompt_text)
    if not prompt_norm:
        return text

    kept: List[str] = []
    for part in _SENTENCE_BOUNDARY_RE.split(text):
        sentence = part.strip()
        if not sentence:
            continue
        sentence_norm = _normalize_for_echo(sentence)
        if (
            sentence_norm
            and difflib.SequenceMatcher(None, sentence_norm, prompt_norm).ratio()
            >= _PROMPT_ECHO_SIMILARITY
        ):
            continue
        kept.append(sentence)
    return " ".join(kept)


def _collapse_repeated_sentences(text: str) -> str:
    """Whisper sometimes repeats the same hallucinated sentence dozens of
    times in a row on silent audio. Normalize whitespace per sentence and
    collapse consecutive duplicates down to one occurrence.
    """
    sentences = [re.sub(r"\s+", " ", part).strip() for part in _SENTENCE_BOUNDARY_RE.split(text)]
    sentences = [s for s in sentences if s]

    collapsed: List[str] = []
    for sentence in sentences:
        if not collapsed or collapsed[-1] != sentence:
            collapsed.append(sentence)
    return " ".join(collapsed)


def _clean_transcript(text: str, prompt_text: Optional[str] = None) -> str:
    """Strip known Whisper hallucination phrases, drop sentences that merely
    echo the decoder prompt text, and collapse immediate sentence repeats, so a
    hallucinated transcript can't slip past the downstream content-length gate
    as if it were real speech.
    """
    if not text:
        return text

    cleaned = text
    for pattern in _HALLUCINATION_PATTERNS:
        cleaned = pattern.sub("", cleaned)

    cleaned = _drop_prompt_echoes(cleaned, prompt_text)
    cleaned = _collapse_repeated_sentences(cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _register_cuda_dll_dirs() -> None:
    """Make the NVIDIA CUDA runtime DLLs (cuBLAS/cuDNN) findable on Windows.

    The nvidia-*-cu12 pip wheels ship the DLLs under site-packages/nvidia/<lib>/bin,
    which is not on the default DLL search path, and ctranslate2 loads them with a
    plain LoadLibrary call that only searches the exe dir and PATH. In a frozen
    (PyInstaller) build the DLLs are bundled into the app directory instead.
    """
    global _cuda_dlls_registered
    if _cuda_dlls_registered or sys.platform != "win32":
        return
    _cuda_dlls_registered = True

    dll_dirs = []
    if getattr(sys, "frozen", False):
        dll_dirs.append(Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)))
    else:
        try:
            import nvidia
        except ImportError:
            return
        for base in nvidia.__path__:
            dll_dirs.extend(p for p in Path(base).glob("*/bin") if p.is_dir())

    for dll_dir in dll_dirs:
        try:
            os.add_dll_directory(str(dll_dir))
        except OSError:
            pass
        # ctranslate2 resolves cublas64_12.dll through the standard search order,
        # which honors PATH but not add_dll_directory alone.
        os.environ["PATH"] = str(dll_dir) + os.pathsep + os.environ.get("PATH", "")


def _is_cuda_library_error(exc: Exception) -> bool:
    """True if the exception looks like a missing CUDA runtime library
    (cuBLAS/cuDNN/etc.), which ctranslate2 raises either when constructing
    the model or later, lazily, on first inference.
    """
    message = str(exc).lower()
    return any(marker in message for marker in _CUDA_ERROR_MARKERS)


class Transcriber:
    def __init__(self, model_size: str, device: str):
        self.model_size = model_size
        self.device = device
        self._cache_key = (model_size, device)
        self._model = None

    def _build_model(self, device: str, on_status=None):
        from faster_whisper import WhisperModel

        # Cache the model under the app folder (not the user's home cache dir)
        # so a portable copy (exe + ffmpeg + config.json + models/) works on any PC.
        models_dir = app_dir() / "models"
        models_dir.mkdir(parents=True, exist_ok=True)

        # Loading by model *name* contacts the Hugging Face Hub for revision
        # revalidation on every load — even with a complete local cache — which
        # stalls the session on slow or blocked networks. Try the local cache
        # first; only a local miss (fresh install, wiped models/) goes online.
        try:
            return WhisperModel(
                self.model_size,
                device=device,
                compute_type="int8",
                download_root=str(models_dir),
                local_files_only=True,
            )
        except Exception as exc:
            # A CUDA failure is not a cache miss — surface it to the CPU
            # fallback instead of pointlessly retrying online.
            if _is_cuda_library_error(exc):
                raise

        if on_status:
            on_status("모델 다운로드 중 (최초 1회)")
        return WhisperModel(
            self.model_size, device=device, compute_type="int8", download_root=str(models_dir)
        )

    def _ensure_model(self, on_status=None):
        if self._model is not None:
            return
        with _MODEL_LOCK:
            cached = _MODEL_CACHE.get(self._cache_key)
            if cached is not None:
                self._model, self.device = cached
                return

            if on_status:
                on_status("모델 로딩 중")
            _register_cuda_dll_dirs()

            try:
                self._model = self._build_model(self.device, on_status=on_status)
            except Exception as exc:
                # ctranslate2 raises a plain Exception/RuntimeError when the CUDA
                # runtime (cuBLAS/cuDNN) is not installed on the machine. Fall back
                # to CPU instead of leaving every job dead for the rest of the session.
                if not _is_cuda_library_error(exc) or self.device == "cpu":
                    raise

                if on_status:
                    on_status("GPU(CUDA) 라이브러리를 찾을 수 없어 CPU로 전환합니다")

                self.device = "cpu"
                self._model = self._build_model("cpu", on_status=on_status)

            _MODEL_CACHE[self._cache_key] = (self._model, self.device)

    def _run_transcribe(self, audio: np.ndarray, topic_keywords: Optional[str]) -> str:
        """Run the actual ctranslate2 compute and materialize the (lazy) segment
        generator. faster_whisper's transcribe() call itself is cheap; the CUDA
        library load happens lazily, on first iteration of the segments
        generator, so any CUDA failure surfaces here rather than in _ensure_model.
        """
        segments, _info = self._model.transcribe(
            audio,
            language="ko",
            # hotwords bias EVERY 30s decoding window. initial_prompt does not:
            # combined with condition_on_previous_text=False, faster-whisper
            # resets the prompt after the first window (prompt_reset_since), so
            # keyword biasing would only cover the first 30s of each segment —
            # and its full-sentence form was what Whisper echoed back verbatim
            # on silent stretches in the first place.
            hotwords=topic_keywords,
            vad_filter=True,
            # A hallucinated window (prompt echo, canned outro) otherwise seeds
            # the decoder context for every following window in the same span.
            condition_on_previous_text=False,
        )
        text = "".join(segment.text for segment in segments).strip()
        return _clean_transcript(text, topic_keywords)

    def transcribe(self, pcm_bytes: bytes, topic_keywords: Optional[str] = None, on_status=None) -> str:
        """Transcribe raw s16le 16kHz mono PCM bytes into Korean text."""
        if not pcm_bytes:
            return ""

        self._ensure_model(on_status=on_status)

        audio = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0

        topic_keywords = (topic_keywords or "").strip() or None

        try:
            return self._run_transcribe(audio, topic_keywords)
        except Exception as exc:
            # The model constructor can succeed even though the CUDA runtime is
            # missing, since ctranslate2 only loads cuBLAS/cuDNN lazily on the
            # first actual inference. Catch that here and retry once on CPU.
            if not _is_cuda_library_error(exc) or self.device == "cpu":
                raise

            if on_status:
                on_status("GPU(CUDA) 라이브러리를 찾을 수 없어 CPU로 전환합니다")

            self.device = "cpu"
            self._model = None
            # The broken CUDA model was already cached by _ensure_model — evict
            # it so the reload (and every later session) builds on CPU instead
            # of being handed the same failing model back.
            with _MODEL_LOCK:
                _MODEL_CACHE.pop(self._cache_key, None)
            self._ensure_model(on_status=on_status)
            return self._run_transcribe(audio, topic_keywords)
