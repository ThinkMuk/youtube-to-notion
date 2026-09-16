"""Configuration contracts for the API-free Codex summarizer."""

import json

import pytest

import yln.config as config_module
from yln.config import ConfigError, load_config, save_summarizer_backend


def write_config(tmp_path, monkeypatch, **overrides):
    raw = {
        "notion_token": "test-notion-token",
        "notion_parent_page_id": "test-page-id",
        "summarizer_backend": "codex",
    }
    raw.update(overrides)
    (tmp_path / "config.json").write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setattr(config_module, "app_dir", lambda: tmp_path)


def test_codex_loads_without_api_keys_with_lightest_defaults(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch)
    config = load_config()
    assert config.summarizer_backend == "codex"
    assert config.codex_model == "gpt-5.6-luna"
    assert config.codex_reasoning_effort == "low"
    assert config.notion_token == "test-notion-token"


def test_codex_model_and_effort_can_be_overridden(tmp_path, monkeypatch):
    write_config(
        tmp_path, monkeypatch, summarizer_backend=" Codex ",
        codex_model="gpt-5.6-sol", codex_reasoning_effort="medium",
    )
    config = load_config()
    assert config.codex_model == "gpt-5.6-sol"
    assert config.codex_reasoning_effort == "medium"


@pytest.mark.parametrize("model", ["", "   ", None, 123])
def test_codex_rejects_invalid_model(tmp_path, monkeypatch, model):
    write_config(tmp_path, monkeypatch, codex_model=model)
    with pytest.raises(ConfigError, match="codex_model"):
        load_config()


@pytest.mark.parametrize("effort", ["", None, "minimal", "typo", 123])
def test_codex_rejects_invalid_reasoning_effort(tmp_path, monkeypatch, effort):
    write_config(tmp_path, monkeypatch, codex_reasoning_effort=effort)
    with pytest.raises(ConfigError, match="codex_reasoning_effort"):
        load_config()


def test_existing_claude_config_still_loads_without_api_keys(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch, summarizer_backend="claude_code", claude_code_model="haiku")
    config = load_config()
    assert config.summarizer_backend == "claude_code"
    assert config.claude_code_model == "haiku"


def test_existing_gemini_default_still_requires_api_key(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch, summarizer_backend=None)
    with pytest.raises(ConfigError, match="gemini_api_key"):
        load_config()


@pytest.mark.parametrize("backend", ["claude_code", "codex"])
def test_save_backend_preserves_credentials_models_and_custom_fields(tmp_path, monkeypatch, backend):
    write_config(tmp_path, monkeypatch, custom="한글 사용자 설정", claude_code_model="sonnet", codex_model="gpt-5.6-luna")
    before = json.loads((tmp_path / "config.json").read_text())
    saved = save_summarizer_backend(backend)
    assert saved.summarizer_backend == backend
    assert load_config().summarizer_backend == backend
    assert json.loads((tmp_path / "config.json").read_text()) == dict(before, summarizer_backend=backend)


def test_save_validates_target_backend_before_writing(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch, summarizer_backend="claude_code", codex_model="")
    before = (tmp_path / "config.json").read_bytes()
    with pytest.raises(ConfigError, match="codex_model"):
        save_summarizer_backend("codex")
    assert (tmp_path / "config.json").read_bytes() == before


def test_save_failure_preserves_previous_selection(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch, summarizer_backend="claude_code")
    before = (tmp_path / "config.json").read_bytes()

    def fail_replace(*args, **kwargs):
        raise PermissionError("write denied")

    monkeypatch.setattr(config_module.Path, "replace", fail_replace)
    with pytest.raises(ConfigError, match="저장"):
        save_summarizer_backend("codex")
    assert (tmp_path / "config.json").read_bytes() == before


def test_save_does_not_create_missing_configuration(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "app_dir", lambda: tmp_path)
    with pytest.raises(ConfigError, match="config.json"):
        save_summarizer_backend("codex")
    assert not (tmp_path / "config.json").exists()


def test_save_rejects_unknown_toggle_value(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch)
    with pytest.raises(ConfigError, match="요약"):
        save_summarizer_backend("unknown")


@pytest.mark.parametrize("value", ["not-a-number", None])
def test_save_reports_invalid_numeric_config_without_changing_file(tmp_path, monkeypatch, value):
    write_config(tmp_path, monkeypatch, summarizer_backend="claude_code", frame_interval_sec=value)
    before = (tmp_path / "config.json").read_bytes()
    with pytest.raises(ConfigError, match="설정 값"):
        save_summarizer_backend("codex")
    assert (tmp_path / "config.json").read_bytes() == before
