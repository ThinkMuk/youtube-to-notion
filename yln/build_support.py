"""Preserve the active app config before PyInstaller replaces the Windows bundle."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _read_object(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"Config must contain a JSON object: {path}")
    return value


def prepare_config(project_dir: Path) -> Path:
    """Snapshot the active settings, or resume a snapshot from a failed build."""
    pending = project_dir / "config.build-pending.json"
    candidates = (
        pending,
        project_dir / "dist" / "YoutubeLiveNotion" / "config.json",
        project_dir / "config.json",
        project_dir / "config.json.example",
    )
    source = next((path for path in candidates if path.is_file()), None)
    if source is None:
        raise FileNotFoundError("No config.json or config.json.example found.")
    config = _read_object(source)
    if source != pending:
        pending.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return source


def restore_config(project_dir: Path) -> Path:
    """Restore the selected backend and fill defaults for newly added settings."""
    pending = project_dir / "config.build-pending.json"
    config = _read_object(pending)
    config.setdefault("summarizer_backend", "codex")
    config.setdefault("codex_model", "gpt-5.6-luna")
    config.setdefault("codex_reasoning_effort", "low")
    destination = project_dir / "dist" / "YoutubeLiveNotion" / "config.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_suffix(".json.tmp")
    staging.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    staging.replace(destination)
    pending.replace(project_dir / "config.build-backup.json")
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("prepare", "restore"))
    args = parser.parse_args()
    project_dir = Path(__file__).resolve().parent.parent
    try:
        path = (prepare_config if args.stage == "prepare" else restore_config)(project_dir)
    except (OSError, ValueError) as exc:
        print(f"[ERROR] Config {args.stage} failed: {exc}")
        return 1
    print(f"Config {args.stage}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
