import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from yln import summarizer as summarizer_module
from yln.summarizer import CodexSummarizer, SummarizerError, build_summarizer


def _successful_process(stdout: str = "진행 로그") -> SimpleNamespace:
    return SimpleNamespace(returncode=0, stdout=stdout, stderr="")


def test_factory_builds_codex_with_configured_model_and_effort() -> None:
    config = SimpleNamespace(
        summarizer_backend="codex",
        codex_model="gpt-5.6-luna",
        codex_reasoning_effort="low",
    )

    result = build_summarizer(config)

    assert isinstance(result, CodexSummarizer)
    assert result.model == "gpt-5.6-luna"
    assert result.reasoning_effort == "low"


def test_call_sends_complete_prompt_on_stdin_and_reads_only_final_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = {}
    transcript = "한국어 강의 전사문입니다. " * 2_000
    monkeypatch.setattr(summarizer_module, "find_codex_command", lambda: ["codex"])

    def fake_run(command, **kwargs):
        output_path = Path(command[command.index("--output-last-message") + 1])
        observed.update(command=command, output_path=output_path, kwargs=kwargs)
        assert not output_path.exists()
        output_path.write_text("최종 요약 결과", encoding="utf-8")
        return _successful_process(stdout="절대로 반환하면 안 되는 진단 로그")

    monkeypatch.setattr(summarizer_module.subprocess, "run", fake_run)

    result = CodexSummarizer()._call("시스템 요약 규칙", transcript, max_tokens=4096)

    assert result == "최종 요약 결과"
    assert transcript in observed["kwargs"]["input"]
    assert "시스템 요약 규칙" in observed["kwargs"]["input"]
    assert "ONLY" in observed["kwargs"]["input"]
    assert observed["kwargs"]["shell"] is False
    assert observed["kwargs"]["cwd"] == str(observed["output_path"].parent)
    command = observed["command"]
    assert command[:4] == ["codex", "exec", "--ignore-user-config", "--model"]
    assert "gpt-5.6-luna" in command
    assert 'model_reasoning_effort="low"' in command
    assert 'approval_policy="never"' in command
    assert "features.shell_tool=false" in command
    assert "features.apps=false" in command
    assert "features.plugins=false" in command
    assert "features.multi_agent=false" in command
    assert 'web_search="disabled"' in command


@pytest.mark.parametrize("first_failure", ["blank", "timeout", "error"])
def test_call_retries_each_transient_failure_in_a_fresh_directory(
    first_failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    call_dirs = []
    sleeps = []
    monkeypatch.setattr(summarizer_module, "find_codex_command", lambda: ["codex"])
    monkeypatch.setattr(summarizer_module.time, "sleep", sleeps.append)

    def fake_run(command, **kwargs):
        call_dirs.append(kwargs["cwd"])
        if len(call_dirs) == 1:
            if first_failure == "timeout":
                raise subprocess.TimeoutExpired(command, 180)
            if first_failure == "error":
                return SimpleNamespace(returncode=2, stdout="stdout 로그", stderr="로그인 필요")
            return _successful_process(stdout="파일 대신 stdout에만 있는 로그")
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text("재시도 성공", encoding="utf-8")
        return _successful_process()

    monkeypatch.setattr(summarizer_module.subprocess, "run", fake_run)

    assert CodexSummarizer()._call("규칙", "전사", max_tokens=1) == "재시도 성공"
    assert len(set(call_dirs)) == 2
    assert sleeps == [2]


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        ("blank", "빈 응답"),
        ("timeout", "시간 초과"),
        ("error", "exit 2"),
    ],
)
def test_call_raises_actionable_korean_error_after_three_failures(
    failure: str, message: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = 0
    monkeypatch.setattr(summarizer_module, "find_codex_command", lambda: ["codex"])
    monkeypatch.setattr(summarizer_module.time, "sleep", lambda _delay: None)

    def fake_run(command, **kwargs):
        nonlocal attempts
        attempts += 1
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 180)
        if failure == "error":
            return SimpleNamespace(returncode=2, stdout="무시할 stdout", stderr="인증 실패")
        return _successful_process(stdout="무시할 stdout")

    monkeypatch.setattr(summarizer_module.subprocess, "run", fake_run)

    with pytest.raises(SummarizerError, match=message):
        CodexSummarizer()._call("규칙", "전사", max_tokens=1)
    assert attempts == 3


def test_call_reports_missing_cli_before_starting_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(summarizer_module, "find_codex_command", lambda: None)
    monkeypatch.setattr(
        summarizer_module.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("CLI가 없으면 subprocess를 실행하면 안 됩니다"),
    )

    with pytest.raises(SummarizerError, match="Codex CLI.*설치"):
        CodexSummarizer()._call("규칙", "전사", max_tokens=1)


def test_call_reports_actionable_tail_of_long_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(summarizer_module, "find_codex_command", lambda: ["codex"])
    monkeypatch.setattr(summarizer_module.time, "sleep", lambda _delay: None)
    long_stderr = ("시작 헤더와 프롬프트 " * 100) + "실제 원인: ChatGPT 인증이 만료되었습니다"
    monkeypatch.setattr(
        summarizer_module.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=2, stdout="", stderr=long_stderr),
    )

    with pytest.raises(SummarizerError, match="실제 원인: ChatGPT 인증이 만료되었습니다"):
        CodexSummarizer()._call("규칙", "전사", max_tokens=1)
