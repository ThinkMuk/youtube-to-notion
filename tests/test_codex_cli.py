import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from yln import codex_cli


def _set_home(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    monkeypatch.setattr(codex_cli.Path, "home", classmethod(lambda cls: home))


def _set_bundled_package(monkeypatch: pytest.MonkeyPatch, executable: Path) -> None:
    package = SimpleNamespace(bundled_codex_path=lambda: executable)
    monkeypatch.setitem(sys.modules, "codex_cli_bin", package)


def test_find_codex_command_prefers_official_bundled_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundled = tmp_path / "collected package" / "bin" / "codex.exe"
    bundled.parent.mkdir(parents=True)
    bundled.write_bytes(b"native")
    _set_bundled_package(monkeypatch, bundled)
    monkeypatch.setattr(
        codex_cli.shutil,
        "which",
        lambda name: pytest.fail("번들 런타임이 있으면 PATH를 조회하면 안 됩니다"),
    )

    assert codex_cli.find_codex_command() == [str(bundled)]


def test_find_codex_command_does_not_use_global_path_when_frozen_bundle_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "codex_cli_bin", None)
    monkeypatch.setattr(codex_cli.sys, "frozen", True, raising=False)
    monkeypatch.setattr(codex_cli.shutil, "which", lambda name: "C:/global/codex.exe")

    with pytest.raises(codex_cli.CodexCLIResolutionError, match="앱.*다시 빌드"):
        codex_cli.find_codex_command()


def test_find_codex_command_reports_broken_collected_package_without_path_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_bundled_package(monkeypatch, tmp_path / "missing" / "codex.exe")
    monkeypatch.setattr(codex_cli.shutil, "which", lambda name: "C:/global/codex.exe")

    with pytest.raises(codex_cli.CodexCLIResolutionError, match="앱.*다시 빌드"):
        codex_cli.find_codex_command()


def test_find_codex_command_returns_native_path_from_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(codex_cli.shutil, "which", lambda name: "/tools/codex.exe" if name == "codex" else None)

    assert codex_cli.find_codex_command() == ["/tools/codex.exe"]


def test_find_codex_command_returns_none_when_cli_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(codex_cli.shutil, "which", lambda name: None)
    monkeypatch.delenv("APPDATA", raising=False)
    _set_home(monkeypatch, tmp_path)

    assert codex_cli.find_codex_command() is None


def test_find_codex_command_prefers_packaged_windows_binary_for_npm_shim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    npm_root = tmp_path / "App Data" / "npm"
    shim = npm_root / "codex.cmd"
    shim.parent.mkdir(parents=True)
    shim.write_text("npm shim", encoding="utf-8")
    native = (
        npm_root
        / "node_modules"
        / "@openai"
        / "codex"
        / "node_modules"
        / "@openai"
        / "codex-win32-x64"
        / "vendor"
        / "x86_64-pc-windows-msvc"
        / "bin"
        / "codex.exe"
    )
    native.parent.mkdir(parents=True)
    native.write_bytes(b"native")
    monkeypatch.setattr(codex_cli.shutil, "which", lambda name: str(shim) if name == "codex" else None)
    monkeypatch.setattr(codex_cli.platform, "machine", lambda: "AMD64")

    assert codex_cli.find_codex_command() == [str(native)]


def test_find_codex_command_rejects_npm_shim_when_native_binary_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    npm_root = tmp_path / "App Data" / "npm"
    shim = npm_root / "codex.cmd"
    script = npm_root / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    shim.parent.mkdir(parents=True)
    shim.write_text("npm shim", encoding="utf-8")
    script.parent.mkdir(parents=True)
    script.write_text("// entry point", encoding="utf-8")
    monkeypatch.setattr(
        codex_cli.shutil,
        "which",
        lambda name: str(shim) if name == "codex" else ("C:/Program Files/nodejs/node.exe" if name == "node" else None),
    )

    with pytest.raises(codex_cli.CodexCLIResolutionError, match="Windows 실행 파일.*재설치"):
        codex_cli.find_codex_command()


def test_find_codex_command_resolves_bat_shim_without_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    npm_root = tmp_path / "npm"
    shim = npm_root / "codex.bat"
    native = (
        npm_root
        / "node_modules"
        / "@openai"
        / "codex"
        / "node_modules"
        / "@openai"
        / "codex-win32-x64"
        / "vendor"
        / "x86_64-pc-windows-msvc"
        / "bin"
        / "codex.exe"
    )
    shim.parent.mkdir(parents=True)
    shim.write_text("npm shim", encoding="utf-8")
    native.parent.mkdir(parents=True)
    native.write_bytes(b"native")
    monkeypatch.setattr(codex_cli.shutil, "which", lambda name: str(shim) if name == "codex" else None)
    monkeypatch.setattr(codex_cli.platform, "machine", lambda: "AMD64")

    assert codex_cli.find_codex_command() == [str(native)]


def test_find_codex_command_supports_historical_vendor_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    npm_root = tmp_path / "npm"
    shim = npm_root / "codex.cmd"
    native = (
        npm_root
        / "node_modules"
        / "@openai"
        / "codex"
        / "vendor"
        / "x86_64-pc-windows-msvc"
        / "codex"
        / "codex.exe"
    )
    shim.parent.mkdir(parents=True)
    shim.write_text("npm shim", encoding="utf-8")
    native.parent.mkdir(parents=True)
    native.write_bytes(b"native")
    monkeypatch.setattr(codex_cli.shutil, "which", lambda name: str(shim) if name == "codex" else None)
    monkeypatch.setattr(codex_cli.platform, "machine", lambda: "AMD64")

    assert codex_cli.find_codex_command() == [str(native)]


def test_find_codex_command_reports_broken_npm_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shim = tmp_path / "npm" / "codex.cmd"
    shim.parent.mkdir(parents=True)
    shim.write_text("npm shim", encoding="utf-8")
    monkeypatch.setattr(codex_cli.shutil, "which", lambda name: str(shim) if name == "codex" else None)

    with pytest.raises(codex_cli.CodexCLIResolutionError, match="재설치"):
        codex_cli.find_codex_command()


def test_open_codex_login_console_runs_resolved_native_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = {}
    monkeypatch.setattr(codex_cli, "find_codex_command", lambda: ["C:/Program Files/YLN/codex.exe"])
    monkeypatch.setattr(codex_cli.sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "CREATE_NEW_CONSOLE", 16, raising=False)

    def fake_popen(command, **kwargs):
        observed.update(command=command, kwargs=kwargs)
        return SimpleNamespace()

    monkeypatch.setattr(codex_cli.subprocess, "Popen", fake_popen)

    assert codex_cli.open_codex_login_console() is None
    assert observed["command"] == ["C:/Program Files/YLN/codex.exe", "login"]
    assert observed["kwargs"] == {"creationflags": 16, "shell": False}


def test_open_codex_login_console_returns_actionable_error_when_runtime_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(codex_cli, "find_codex_command", lambda: None)

    error = codex_cli.open_codex_login_console()

    assert error is not None
    assert "Codex" in error
    assert "다시 빌드" in error


def test_open_codex_login_console_returns_launch_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(codex_cli, "find_codex_command", lambda: ["codex.exe"])
    monkeypatch.setattr(
        codex_cli.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("실행 차단")),
    )

    error = codex_cli.open_codex_login_console()

    assert error is not None
    assert "실행 차단" in error
