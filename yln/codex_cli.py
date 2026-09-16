"""Locate a Codex CLI command that can be executed without a shell."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Tuple


class CodexCLIResolutionError(RuntimeError):
    """Raised when a discovered Codex installation cannot be used safely."""


def _windows_targets() -> Sequence[Tuple[str, str]]:
    machine = platform.machine().lower()
    arm64 = ("codex-win32-arm64", "aarch64-pc-windows-msvc")
    x64 = ("codex-win32-x64", "x86_64-pc-windows-msvc")
    if machine in {"arm64", "aarch64"}:
        return (arm64, x64)
    return (x64, arm64)


def _packaged_windows_binary(package_root: Path) -> Optional[Path]:
    for package_name, target in _windows_targets():
        package_roots = (
            package_root / "node_modules" / "@openai" / package_name,
            package_root.parent / package_name,
            package_root,
        )
        for platform_package in package_roots:
            vendor_target = platform_package / "vendor" / target
            for relative in (("bin", "codex.exe"), ("codex", "codex.exe")):
                binary = vendor_target.joinpath(*relative)
                if binary.is_file():
                    return binary
    return None


def _resolve_npm_shim(shim: Path) -> List[str]:
    package_root = shim.parent / "node_modules" / "@openai" / "codex"
    native = _packaged_windows_binary(package_root)
    if native is not None:
        return [str(native)]

    raise CodexCLIResolutionError(
        "Codex npm 설치의 Windows 실행 파일(codex.exe)을 찾을 수 없습니다. "
        "`npm install -g @openai/codex@latest`로 Codex CLI를 재설치해 주세요."
    )


def _command_for_path(path: str) -> List[str]:
    candidate = Path(path)
    if candidate.suffix.lower() in {".cmd", ".bat"}:
        return _resolve_npm_shim(candidate)
    return [path]


def _bundled_codex_command() -> Optional[List[str]]:
    try:
        from codex_cli_bin import bundled_codex_path
    except ImportError as e:
        if getattr(sys, "frozen", False):
            raise CodexCLIResolutionError(
                "앱에 Codex 실행 파일 패키지가 포함되지 않았습니다. `build.bat`으로 앱을 다시 빌드해 주세요."
            ) from e
        return None

    try:
        executable = Path(bundled_codex_path())
    except Exception as e:  # noqa: BLE001 - package locator errors become user-facing rebuild guidance
        raise CodexCLIResolutionError(
            f"앱에 포함된 Codex 실행 파일을 확인하지 못했습니다: {e}. `build.bat`으로 앱을 다시 빌드해 주세요."
        ) from e
    if not executable.is_file():
        raise CodexCLIResolutionError(
            "앱에 포함된 Codex 실행 파일이 없거나 손상되었습니다. `build.bat`으로 앱을 다시 빌드해 주세요."
        )
    return [str(executable)]


def find_codex_command() -> Optional[List[str]]:
    """Return an argv prefix for Codex, resolving Windows npm shims safely."""
    bundled = _bundled_codex_command()
    if bundled is not None:
        return bundled

    path = shutil.which("codex")
    if path:
        return _command_for_path(path)

    appdata = os.environ.get("APPDATA")
    if appdata:
        npm_shim = Path(appdata) / "npm" / "codex.cmd"
        if npm_shim.is_file():
            return _resolve_npm_shim(npm_shim)

    native_fallback = Path.home() / ".local" / "bin" / "codex.exe"
    if native_fallback.is_file():
        return [str(native_fallback)]
    return None


def open_codex_login_console() -> Optional[str]:
    """Open `codex login` in a console; return a Korean error or None on success."""
    try:
        command = find_codex_command()
    except CodexCLIResolutionError as e:
        return str(e)
    if command is None:
        return "Codex 실행 파일을 찾을 수 없습니다. `build.bat`으로 앱을 다시 빌드해 주세요."

    creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0) if sys.platform == "win32" else 0
    try:
        subprocess.Popen([*command, "login"], creationflags=creationflags, shell=False)
    except OSError as e:
        return f"Codex 로그인 창을 실행하지 못했습니다: {e}"
    return None
