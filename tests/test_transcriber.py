"""Tests for yln.transcriber's transcript cleaning and decode options.

Covers the prompt-echo filter: Whisper hallucinates its decoder-prompt text
back as transcription on silent stretches — observed verbatim in production
transcript sub-pages (Slide 25/26, 2026-08-05 lecture) with the old
sentence-form initial_prompt — and those echoes must not survive past the
cleaner, or they masquerade as real content downstream. Also covers the
hotwords decode contract (keyword biasing must reach every window).
"""

import sys
import types

import numpy as np
import pytest

import yln.transcriber as transcriber_mod
from yln.transcriber import Transcriber, _clean_transcript

# 실측 반향 문장과 동일한 형태의 프롬프트 (과거 initial_prompt 시절 형태).
PROMPT = "이 강의는 AI,ML,DL,데이터, 가설공간, 모델, 학습, learning 관련 기술 강의입니다."

# hotwords 전환 후 transcriber가 반향 필터 기준으로 쓰는 값 (키워드 나열 그대로).
KEYWORDS = "AI,ML,DL,데이터, 가설공간, 모델, 학습, learning"


def test_exact_prompt_echo_is_dropped() -> None:
    assert _clean_transcript(PROMPT, PROMPT) == ""


def test_echo_mixed_with_real_content_keeps_only_real_content() -> None:
    text = f"{PROMPT} 정밀도와 재현율의 조화평균인 F1 스코어를 봐야 합니다."
    result = _clean_transcript(text, PROMPT)
    assert "F1" in result
    assert "기술 강의입니다" not in result


def test_fuzzy_echo_with_different_punctuation_is_dropped() -> None:
    # 반향은 쉼표/띄어쓰기가 조금씩 달라질 수 있다 — 정규화 후 유사도로 걸러야 한다.
    fuzzy = "이 강의는 AI, ML, DL, 데이터 가설공간 모델 학습 learning 관련 기술 강의입니다"
    assert _clean_transcript(fuzzy, PROMPT) == ""


def test_repeated_echoes_are_all_dropped() -> None:
    text = f"{PROMPT} {PROMPT} {PROMPT}"
    assert _clean_transcript(text, PROMPT) == ""


def test_real_sentence_mentioning_keywords_is_kept() -> None:
    # 키워드가 등장하는 실제 강의 문장은 반향이 아니다 — 지우면 안 된다.
    text = "오늘은 AI와 ML의 차이, 그리고 DL 모델 학습 과정을 살펴봅니다."
    assert _clean_transcript(text, PROMPT) == text


def test_without_prompt_nothing_is_dropped() -> None:
    assert _clean_transcript(PROMPT, None) == PROMPT


def test_existing_outro_hallucination_filter_still_works() -> None:
    # 기존 아웃트로 환각 필터가 반향 필터 추가 후에도 그대로 동작해야 한다.
    text = "시청해 주셔서 감사합니다"
    assert _clean_transcript(text, PROMPT) == ""


def test_keyword_list_echo_is_dropped() -> None:
    # hotwords 전환 후 반향은 문장형이 아니라 키워드 나열형으로 나타날 수 있다.
    echoed = "AI, ML, DL, 데이터, 가설공간, 모델, 학습, learning."
    assert _clean_transcript(echoed, KEYWORDS) == ""


def test_real_sentence_is_kept_against_keyword_prompt() -> None:
    # 같은 키워드를 언급하는 실제 강의 문장은 키워드 나열 기준으로도 반향이 아니다.
    text = "오늘은 AI와 ML의 차이, 그리고 DL 모델 학습 과정을 살펴봅니다."
    assert _clean_transcript(text, KEYWORDS) == text


class _FakeModel:
    """WhisperModel.transcribe 호출 인자를 기록하는 더블."""

    def __init__(self):
        self.kwargs = None

    def transcribe(self, audio, **kwargs):
        self.kwargs = kwargs
        return iter(()), None


# ---------------------------------------------------------------------------
# 모델 로딩: 로컬 캐시 우선(local_files_only) + 프로세스 내 세션 간 모델 재사용
# ---------------------------------------------------------------------------


class _FakeWhisperFactory:
    """faster_whisper.WhisperModel 생성/추론 호출을 기록·제어하는 더블 팩토리."""

    def __init__(self):
        self.calls = []
        self.local_load_error = None  # local_files_only=True 시도에서 던질 예외
        self.init_error_devices = set()  # 이 device는 생성 시 CUDA 라이브러리 예외
        self.transcribe_error_devices = set()  # 이 device는 첫 추론 시 CUDA 예외
        factory = self

        class FakeWhisperModel:
            def __init__(
                self,
                model_size,
                device=None,
                compute_type=None,
                download_root=None,
                local_files_only=False,
            ):
                factory.calls.append(
                    {
                        "model_size": model_size,
                        "device": device,
                        "download_root": download_root,
                        "local_files_only": local_files_only,
                    }
                )
                if device in factory.init_error_devices:
                    raise RuntimeError("Library cublas64_12.dll is not found")
                if local_files_only and factory.local_load_error is not None:
                    raise factory.local_load_error
                self.device = device

            def transcribe(self, audio, **kwargs):
                if self.device in factory.transcribe_error_devices:
                    raise RuntimeError("Unable to load cudnn_ops64_9.dll")
                return iter(()), None

        self.WhisperModel = FakeWhisperModel


@pytest.fixture
def fake_whisper(monkeypatch, tmp_path):
    factory = _FakeWhisperFactory()
    monkeypatch.setitem(
        sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=factory.WhisperModel)
    )
    monkeypatch.setattr(transcriber_mod, "app_dir", lambda: tmp_path)
    monkeypatch.setattr(transcriber_mod, "_MODEL_CACHE", {}, raising=False)
    return factory


def test_local_cache_tried_first_without_network(fake_whisper) -> None:
    # 모델 이름 로드는 캐시가 완전해도 매번 Hub에 재검증을 나가 세션 시작을 세운다 —
    # 로컬 캐시(local_files_only=True) 시도가 반드시 먼저여야 한다.
    t = Transcriber("medium", "cpu")
    t._ensure_model()
    assert len(fake_whisper.calls) == 1
    assert fake_whisper.calls[0]["local_files_only"] is True


def test_missing_local_cache_falls_back_to_online_download(fake_whisper) -> None:
    # 재빌드로 models\가 비었을 때: 로컬 시도 실패 → 온라인 다운로드로 폴백하고
    # 사용자에게 다운로드 중임을 알려야 한다.
    fake_whisper.local_load_error = FileNotFoundError("snapshot not found locally")
    statuses = []
    t = Transcriber("medium", "cpu")
    t._ensure_model(on_status=statuses.append)
    assert [c["local_files_only"] for c in fake_whisper.calls] == [True, False]
    assert any("다운로드" in s for s in statuses)


def test_model_is_reused_across_instances(fake_whisper) -> None:
    # 새 세션은 새 Transcriber를 만든다 — 같은 프로세스에서 두 번째 세션부터는
    # 모델을 다시 로드하지 말고 재사용해야 한다.
    t1 = Transcriber("medium", "cpu")
    t1._ensure_model()
    t2 = Transcriber("medium", "cpu")
    statuses = []
    t2._ensure_model(on_status=statuses.append)
    assert len(fake_whisper.calls) == 1
    assert t2._model is t1._model
    # 캐시 적중은 즉시 끝난다 — "모델 로딩 중" 문구가 떠서는 안 된다.
    assert statuses == []


def test_different_model_size_is_not_shared(fake_whisper) -> None:
    t1 = Transcriber("medium", "cpu")
    t1._ensure_model()
    t2 = Transcriber("small", "cpu")
    t2._ensure_model()
    assert len(fake_whisper.calls) == 2
    assert t1._model is not t2._model


def test_construction_cuda_error_falls_back_to_cpu_and_caches_cpu(fake_whisper) -> None:
    # 생성 시 CUDA 라이브러리 부재: 온라인 재다운로드가 아니라 CPU 전환으로 대응하고,
    # 이후 세션은 CUDA 재시도 없이 CPU 모델을 그대로 재사용해야 한다.
    fake_whisper.init_error_devices = {"cuda"}
    statuses = []
    t1 = Transcriber("medium", "cuda")
    t1._ensure_model(on_status=statuses.append)
    assert [c["device"] for c in fake_whisper.calls] == ["cuda", "cpu"]
    assert all(c["local_files_only"] for c in fake_whisper.calls)
    assert t1.device == "cpu"
    assert any("CPU" in s for s in statuses)

    t2 = Transcriber("medium", "cuda")
    t2._ensure_model()
    assert len(fake_whisper.calls) == 2
    assert t2._model is t1._model
    assert t2.device == "cpu"


def test_inference_cuda_error_evicts_cache_and_retries_on_cpu(fake_whisper) -> None:
    # ctranslate2는 CUDA 라이브러리를 첫 추론에서야 로드한다 — 그때 실패하면
    # 캐시에 남은 (깨진) CUDA 모델을 비우고 CPU로 다시 로드해야 한다.
    fake_whisper.transcribe_error_devices = {"cuda"}
    t1 = Transcriber("medium", "cuda")
    result = t1.transcribe(b"\x00\x00")
    assert result == ""
    assert [c["device"] for c in fake_whisper.calls] == ["cuda", "cpu"]

    t2 = Transcriber("medium", "cuda")
    t2._ensure_model()
    assert len(fake_whisper.calls) == 2
    assert t2._model is t1._model
    assert t2.device == "cpu"


def test_run_transcribe_uses_hotwords_not_initial_prompt() -> None:
    # 키워드 보정은 hotwords로 매 30초 창에 주입돼야 한다 — initial_prompt는
    # condition_on_previous_text=False와 조합 시 첫 창 이후 리셋되고, 문장형
    # 프롬프트는 무음 구간에서 문장째 반향된 실측 이력이 있다.
    transcriber = Transcriber("base", "cpu")
    fake = _FakeModel()
    transcriber._model = fake

    result = transcriber._run_transcribe(np.zeros(16000, dtype=np.float32), KEYWORDS)

    assert result == ""
    assert fake.kwargs["hotwords"] == KEYWORDS
    assert fake.kwargs["condition_on_previous_text"] is False
    assert "initial_prompt" not in fake.kwargs
