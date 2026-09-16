"""Protect the active Windows app configuration across a PyInstaller rebuild."""

import json

import pytest

from yln.build_support import prepare_config, restore_config


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def test_rebuild_preserves_active_settings_and_selected_backend(tmp_path):
    active = tmp_path / "dist" / "YoutubeLiveNotion" / "config.json"
    original = {
        "notion_token": "keep-token", "notion_parent_page_id": "keep-page",
        "whisper_model": "large-v3", "custom_setting": "보존",
        "summarizer_backend": "claude_code", "claude_code_model": "haiku",
    }
    write_json(active, original)
    write_json(tmp_path / "config.json", {"notion_token": "stale-root-token"})
    prepare_config(tmp_path)
    active.write_text("simulated build replacement", encoding="utf-8")
    restore_config(tmp_path)
    result = json.loads(active.read_text(encoding="utf-8"))
    assert result == dict(original, codex_model="gpt-5.6-luna", codex_reasoning_effort="low")
    assert json.loads((tmp_path / "config.build-backup.json").read_text()) == original


def test_rebuild_keeps_explicit_model_settings(tmp_path):
    original = {
        "summarizer_backend": "codex", "codex_model": "gpt-5.6-sol",
        "codex_reasoning_effort": "medium",
    }
    write_json(tmp_path / "config.json", original)
    prepare_config(tmp_path)
    restore_config(tmp_path)
    result = json.loads((tmp_path / "dist" / "YoutubeLiveNotion" / "config.json").read_text())
    assert result == original


def test_failed_build_snapshot_survives_retry_with_stale_root_config(tmp_path):
    write_json(tmp_path / "config.build-pending.json", {"notion_token": "latest"})
    write_json(tmp_path / "config.json", {"notion_token": "stale"})
    prepare_config(tmp_path)
    restore_config(tmp_path)
    result = json.loads((tmp_path / "dist" / "YoutubeLiveNotion" / "config.json").read_text())
    assert result["notion_token"] == "latest"


@pytest.mark.parametrize("filename", ["config.json", "config.json.example"])
def test_first_build_uses_root_config_or_example(tmp_path, filename):
    write_json(tmp_path / filename, {"notion_token": "root-or-example"})
    prepare_config(tmp_path)
    restore_config(tmp_path)
    result = json.loads((tmp_path / "dist" / "YoutubeLiveNotion" / "config.json").read_text())
    assert result["notion_token"] == "root-or-example"
    assert result["summarizer_backend"] == "codex"


@pytest.mark.parametrize("contents", ["broken-json", "[]"])
def test_invalid_active_config_stops_before_overwriting_backup(tmp_path, contents):
    active = tmp_path / "dist" / "YoutubeLiveNotion" / "config.json"
    active.parent.mkdir(parents=True)
    active.write_text(contents)
    backup = tmp_path / "config.build-backup.json"
    backup.write_text('{"notion_token": "saved"}')
    with pytest.raises(ValueError):
        prepare_config(tmp_path)
    assert active.read_text() == contents
    assert backup.read_text() == '{"notion_token": "saved"}'
    assert not (tmp_path / "config.build-pending.json").exists()


def test_restore_requires_prepared_snapshot(tmp_path):
    with pytest.raises(FileNotFoundError):
        restore_config(tmp_path)
