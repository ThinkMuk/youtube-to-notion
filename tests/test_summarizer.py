"""Tests for yln.summarizer's response validation.

Covers `_postprocess_bullets` (slide-level bullet parsing + rejection/sentinel
filtering) and `Summarizer.summarize_lecture` (empty-input short-circuit and
full-summary rejection fallback). The rejection-fixture texts below are the
actual apology/refusal strings that were published to Notion in production
before this validation existed (76 sections, 15 false publishes, 19.7%).
"""

from typing import List

import pytest

from yln.summarizer import Summarizer, _postprocess_bullets


# ---------------------------------------------------------------------------
# _postprocess_bullets: real failure strings observed in production Notion
# pages. All of these must be rejected (-> None) so the caller carries the
# transcript over instead of publishing an apology/refusal.
# ---------------------------------------------------------------------------

REJECTION_FIXTURES = [
    (
        "전사본 매우 불완전",
        '전사본이 매우 불완전하여 의미 있는 내용을 추출하기 어렵습니다. '
        '"1,2,3 시리즈가 여기에 들어가는 거고, 그 다음에"로 끝나 핵심 내용이 누락되었습니다.',
    ),
    (
        "스크립트 제공 요청",
        "실제 변환할 강의 스크립트를 제공해 주세요.\n"
        "현재는 강의 주제 키워드만 있고, 정리해야 할 음성인식 텍스트(스크립트)가 없습니다.\n"
        "변환할 스크립트를 붙여주세요.",
    ),
    (
        "어시스턴트 인사 + 안내",
        "안녕하세요! 강의 노트 작성 어시스턴트 준비가 되어 있습니다.\n"
        "다만 현재 처리할 강의 텍스트가 보이지 않습니다. 다음 정보를 제공해 주세요:\n"
        "1. **음성인식 원본 텍스트** - 강의 슬라이드가 표시된 시간 동안\n"
        "강의 텍스트를 복사하여 붙여넣어 주세요!",
    ),
    (
        "transcript 미수신 (한/영 혼합)",
        "실제 강의 음성-텍스트 transcript를 받지 못했습니다. 처리해야 할 강의 내용을 제공해 주세요.\n"
        "구조:\n```\n[강의 주제 키워드: ...]\n[실제 음성 변환 텍스트 여기에 붙여넣기]\n```",
    ),
    (
        "영문 거부 (한글 키워드 혼입)",
        "The provided text only contains repeated statements about lecture topics "
        "(AI, 데이터 시각화, 전처리, EDA, Python) rather than an actual speech-to-text "
        "transcript of what was spoken during a slide presentation.",
    ),
    (
        "영문 거부 2",
        "No actual transcript content provided. The text you shared only repeats the "
        "lecture topic metadata (AI, 데이터 시각화, 전처리, EDA, Python) without any actual "
        "speech-to-text transcript to condense.",
    ),
]


@pytest.mark.parametrize(
    "text", [text for _, text in REJECTION_FIXTURES], ids=[name for name, _ in REJECTION_FIXTURES]
)
def test_postprocess_bullets_rejects_known_failure_strings(text: str) -> None:
    assert _postprocess_bullets(text) is None


# ---------------------------------------------------------------------------
# NO_CONTENT verdict: a definitive "nothing to summarize" judgment (sentinel
# line or natural-language phrasing), distinct from an invalid response —
# returns [] so the caller carries the group over without burning a retry
# (which used to end in a placeholder section after the retry budget ran out).
# ---------------------------------------------------------------------------

# CLI 백엔드가 sentinel 뒤에 판정 사유 문단을 덧붙인 실측 형태 (100자 이상 —
# 과거의 부분 문자열 + len<100 판정으로는 미탐되어 불릿으로 발행되던 케이스).
_SENTINEL_WITH_RATIONALE = (
    "NO_CONTENT\n"
    "이 구간의 전사는 강의 주제 키워드 문장이 반복된 것으로 보이며 실제 강의 발화가 "
    "포함되어 있지 않아 요약할 수 있는 기술적 내용이 존재하지 않습니다. "
    "따라서 위와 같이 판정하였습니다."
)


@pytest.mark.parametrize(
    "text",
    [
        "NO_CONTENT",
        "NO_CONTENT\n(요약할 내용이 없습니다)",
        "no_content",
        "NO CONTENT",
        "- NO_CONTENT",
        _SENTINEL_WITH_RATIONALE,
    ],
    ids=[
        "sentinel 단독",
        "sentinel + 잡담",
        "소문자",
        "언더바 없음",
        "불릿 장식",
        "sentinel + 100자 이상 판정 사유",
    ],
)
def test_postprocess_bullets_returns_empty_list_for_no_content_sentinel(text: str) -> None:
    assert _postprocess_bullets(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "이 구간에는 요약할 만한 강의 내용이 없습니다.",
        "요약할 내용이 없습니다.",
        "전사본에서 추출할 수 있는 내용이 없습니다.",
        "No content to summarize.",
        '이 음성 전사본은 강의 내용이 없습니다. "다시 어디로 가버린다?"는 강사의 주저리는 표현으로, '
        "추출할 수 있는 주요 내용이 없습니다.",
    ],
    ids=["만한+명사 삽입", "기본형", "추출할 수 있는", "영어 판정", "실측 운영 문구"],
)
def test_postprocess_bullets_treats_natural_language_no_content_as_verdict(text: str) -> None:
    # sentinel 철자를 지키지 않은 "내용 없음" 판정도 거부(None, 재시도 대상)가
    # 아니라 NO_CONTENT([])다 — 재시도해도 같은 결과라 예산만 태우고 placeholder로
    # 끝나던 경로를 막는다.
    assert _postprocess_bullets(text) == []


def test_postprocess_bullets_keeps_bullet_containing_no_content_as_substring() -> None:
    # 정당한 기술 불릿 속의 NO_CONTENT(HTTP 204)는 판정이 아니다 — 줄 전체가
    # sentinel일 때만 판정으로 봐야 한다.
    text = "HTTP 204 NO_CONTENT 응답은 본문 없이 상태만 반환"
    assert _postprocess_bullets(text) == [text]


def test_postprocess_bullets_rejects_english_only_response() -> None:
    # 한글이 전혀 없는 응답은 정당한 요약일 수 없다.
    text = "This is an English-only response about data visualization."
    assert _postprocess_bullets(text) is None


# ---------------------------------------------------------------------------
# _postprocess_bullets: normal, valid responses must survive intact.
# ---------------------------------------------------------------------------

def test_postprocess_bullets_splits_plain_lines() -> None:
    text = "Seaborn은 데이터프레임을 자동으로 인식하고 처리 가능\nMatplotlib보다 간결한 문법 제공"
    assert _postprocess_bullets(text) == [
        "Seaborn은 데이터프레임을 자동으로 인식하고 처리 가능",
        "Matplotlib보다 간결한 문법 제공",
    ]


def test_postprocess_bullets_strips_bullet_marker() -> None:
    text = "• Seaborn은 데이터프레임을 자동으로 인식하고 처리 가능"
    assert _postprocess_bullets(text) == ["Seaborn은 데이터프레임을 자동으로 인식하고 처리 가능"]


def test_postprocess_bullets_strips_all_prefix_styles_and_caps_at_six() -> None:
    text = (
        "- 첫 번째 항목\n* 두 번째 항목\n1. 세 번째 항목\n③ 네 번째 항목\n"
        "- 다섯 번째 항목\n- 여섯 번째 항목\n- 일곱 번째 항목"
    )
    assert _postprocess_bullets(text) == [
        "첫 번째 항목",
        "두 번째 항목",
        "세 번째 항목",
        "네 번째 항목",
        "다섯 번째 항목",
        "여섯 번째 항목",
    ]


def test_postprocess_bullets_cleans_escapes_and_bold_markers() -> None:
    text = "\\*\\*중요\\*\\* 결측치는 dropna()로 제거"
    assert _postprocess_bullets(text) == ["중요 결측치는 dropna()로 제거"]


def test_postprocess_bullets_dedupes_repeated_lines() -> None:
    text = "표본이 작으면 평균 신뢰도가 낮다\n표본이 작으면 평균 신뢰도가 낮다"
    assert _postprocess_bullets(text) == ["표본이 작으면 평균 신뢰도가 낮다"]


def test_postprocess_bullets_does_not_reject_long_summary_containing_bunyeoneoh() -> None:
    # "붙여넣" 단어 자체가 아니라 짧고 특이도 높은 거부 문구만 걸러야 한다.
    # 이 픽스처는 400자 이상으로, 길이 가드가 없다면 오탐(None)이 났을 문구다.
    text = (
        "실습 진행 안내 시간에 미리 제공된 노트북 파일에 이미 작성된 코드를 직접 타이핑하지 않고 "
        "그대로 복사해 붙여넣어 실행하는 방식으로 진행함을 설명하며, 이번 실습에서는 데이터 "
        "전처리부터 결측치 처리와 범주형 변수 인코딩, 시각화까지 전체 파이프라인을 순서대로 "
        "다룰 예정이라고 안내한다.\n"
        "Titanic 데이터셋을 pandas의 read_csv 함수로 로드하고 head()로 데이터프레임의 앞부분 구조를 확인\n"
        "info()와 describe()로 각 컬럼의 데이터 타입과 기초 통계량을 확인하며 결측치가 있는 컬럼을 파악\n"
        "Age 컬럼의 결측치는 중앙값으로 대체하고 Cabin 컬럼은 결측 비율이 지나치게 높아 제거하기로 결정\n"
        "Sex와 Embarked 범주형 변수는 원핫 인코딩으로 변환해 모델 입력으로 사용할 수 있도록 전처리"
    )
    assert len(text) >= 400, "fixture must be long enough to exercise the length guard"
    result = _postprocess_bullets(text)
    assert result is not None
    assert len(result) <= 6


# ---------------------------------------------------------------------------
# summarize_lecture: empty-input short circuit + full-summary rejection guard.
# ---------------------------------------------------------------------------

class _DummySummarizer(Summarizer):
    """Test double: `_call` returns a canned response and records invocation."""

    def __init__(self, response: str):
        self.response = response
        self.called = False

    def _call(self, system: str, user_content: str, max_tokens: int) -> str:
        self.called = True
        return self.response


def test_summarize_slide_returns_empty_list_for_empty_transcript() -> None:
    # 빈 전사는 "요약할 내용 없음" 확정([])이지 무효 응답(None)이 아니다.
    dummy = _DummySummarizer(response="이 값은 사용되지 않아야 한다")
    assert dummy.summarize_slide("") == []
    assert dummy.summarize_slide("   ") == []
    assert dummy.called is False


def test_summarize_lecture_skips_llm_call_when_no_slide_summaries() -> None:
    dummy = _DummySummarizer(response="이 값은 사용되지 않아야 한다")
    result = dummy.summarize_lecture([], title="빈 강의")
    assert result == "(요약할 음성 내용이 없습니다)"
    assert dummy.called is False


def test_summarize_lecture_falls_back_when_response_is_a_refusal() -> None:
    dummy = _DummySummarizer(
        response="실제 변환할 강의 스크립트를 제공해 주세요. 정리해야 할 음성인식 텍스트가 없습니다."
    )
    slide_summaries: List[List[str]] = [["더미 슬라이드 요약"]]
    result = dummy.summarize_lecture(slide_summaries, title="더미 강의")
    assert result == "(전체 요약 생성에 실패했습니다 — 슬라이드 요약을 참고해 주세요)"
    assert dummy.called is True


def test_summarize_lecture_returns_llm_text_when_valid() -> None:
    dummy = _DummySummarizer(response="주제\n테스트 강의\n\n핵심 개념\n- 핵심 1\n\n정리\n요약 끝")
    slide_summaries: List[List[str]] = [["더미 슬라이드 요약"]]
    result = dummy.summarize_lecture(slide_summaries, title="더미 강의")
    assert result == dummy.response
    assert dummy.called is True
