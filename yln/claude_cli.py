"""Claude Code CLI management helpers.

This module supports user-triggered lifecycle actions for the local Claude
Code CLI (`claude`), used by the `claude_code` summarizer backend:

- locating the installed CLI (shared with `yln.summarizer.ClaudeCodeSummarizer`)
- opening an interactive console for the user to install / log in
- a best-effort "초기화" (reset) cleanup that removes personal data left
  behind by the CLI and by this app, intended for shared/lab PCs where the
  next user should not inherit a previous person's Claude login or Notion
  credentials.

Everything here is best-effort: failures are reported back to the caller as
strings rather than raised, since this is cleanup/convenience tooling, not a
critical path for the app's core recording flow.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import List, Optional

try:
    import winreg
except ImportError:  # non-Windows dev environments
    winreg = None  # type: ignore[assignment]


def find_claude_cli() -> Optional[str]:
    """Locate the installed `claude` CLI executable, if any.

    Checks PATH first (`shutil.which`), then falls back to the native
    installer's default location (`~/.local/bin/claude.exe`) in case PATH
    was not updated yet (fresh install, or the app was launched before a
    new terminal picked up the change).
    """
    path = shutil.which("claude")
    if path:
        return path

    default = Path.home() / ".local" / "bin" / "claude.exe"
    if default.exists():
        return str(default)

    return None


def open_login_console() -> bool:
    """Launch a new console window running `claude /login` for the user to
    complete interactively (browser-based auth). Returns False if the CLI
    is not installed (caller should offer `open_install_console` instead)."""
    cli = find_claude_cli()
    if not cli:
        return False

    subprocess.Popen(
        [cli, "/login"],
        creationflags=subprocess.CREATE_NEW_CONSOLE,
    )
    return True


def open_install_console() -> None:
    """Launch a new PowerShell console running the official Claude Code CLI
    installer. The user completes installation (and can then log in) in
    that window."""
    subprocess.Popen(
        ["powershell", "-NoExit", "-Command", "irm https://claude.ai/install.ps1 | iex"],
        creationflags=subprocess.CREATE_NEW_CONSOLE,
    )


def _delete_file(path: Path, description: str, results: List[str]) -> None:
    try:
        if path.exists():
            path.unlink()
            results.append(f"{description} 삭제 완료 ({path})")
    except OSError as e:
        results.append(f"[실패] {description} 삭제 실패 ({path}): {e}")


def _delete_tree(path: Path, description: str, results: List[str]) -> None:
    try:
        if path.exists():
            shutil.rmtree(path)
            results.append(f"{description} 삭제 완료 ({path})")
    except OSError as e:
        results.append(f"[실패] {description} 삭제 실패 ({path}): {e}")


def _remove_local_bin_from_user_path(results: List[str]) -> None:
    if winreg is None:
        return

    target = str(Path.home() / ".local" / "bin")

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
            try:
                current_value, value_type = winreg.QueryValueEx(key, "Path")
            except FileNotFoundError:
                return  # no user PATH set at all - nothing to do

            entries = current_value.split(";")
            kept = [e for e in entries if e.strip().strip('"').rstrip("\\").lower() != target.rstrip("\\").lower()]

            if len(kept) == len(entries):
                return  # not present - skip silently

            new_value = ";".join(kept)
            winreg.SetValueEx(key, "Path", 0, value_type, new_value)
            results.append(f"사용자 PATH 환경변수에서 '{target}' 제거 완료")
    except OSError as e:
        results.append(f"[실패] 사용자 PATH 환경변수 정리 실패: {e}")


def uninstall(app_config_path: Path) -> List[str]:
    """Best-effort removal of personal data left by the Claude Code CLI and
    by this app. Intended for shared/lab PCs so the next user does not
    inherit a previous person's Claude login or Notion credentials.

    Never raises: every step is wrapped so one failure does not block the
    rest. Returns a list of Korean-facing result lines, one per step
    (success or "[실패] ..." on failure), suitable for appending directly to
    the app's status log.
    """
    results: List[str] = []
    home = Path.home()
    claude_dir = home / ".claude"
    local_bin = home / ".local" / "bin"

    # 1. CLI login credentials.
    _delete_file(claude_dir / ".credentials.json", "Claude CLI 로그인 정보", results)

    # 2. Entire ~/.claude directory (sessions, settings, and any other
    #    Claude Code usage history stored there).
    _delete_tree(claude_dir, "Claude CLI 세션/설정 데이터(~/.claude)", results)

    # 3. The CLI binary itself and any shim files.
    _delete_file(local_bin / "claude.exe", "Claude CLI 프로그램(claude.exe)", results)
    _delete_file(local_bin / "claude", "Claude CLI 셸 스크립트(claude)", results)

    # 4. Remove ~/.local/bin from the user PATH.
    _remove_local_bin_from_user_path(results)

    # 5. The app's own config.json (Notion token, API keys).
    _delete_file(Path(app_config_path), "앱 설정 파일(config.json, Notion 토큰·API 키 포함)", results)

    # 6. Captured session frames/audio (recording cache). `new_session_dir()`
    #    in yln/capture.py creates (and returns) a fresh timestamped dir
    #    under `<tempdir>/yln_sessions/<timestamp>`; we remove the whole
    #    `yln_sessions` root rather than calling that function, so this step
    #    doesn't itself create a new empty session directory first.
    try:
        import tempfile

        sessions_root = Path(tempfile.gettempdir()) / "yln_sessions"
        _delete_tree(sessions_root, "녹화 캐시(캡처된 슬라이드/오디오)", results)
    except Exception as e:  # noqa: BLE001 - best-effort, never raise
        results.append(f"[실패] 녹화 캐시 삭제 실패: {e}")

    return results
