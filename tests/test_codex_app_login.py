from types import SimpleNamespace

import pytest

from yln import app as app_module
from yln.app import App
from yln.config import ConfigError


class _Control:
    def __init__(self, value="") -> None:
        self.value = value
        self.options = {}

    def get(self):
        return self.value

    def set(self, value) -> None:
        self.value = value

    def config(self, **kwargs) -> None:
        self.options.update(kwargs)


class _Widget(_Control):
    created = []

    def __init__(self, parent=None, **kwargs) -> None:
        super().__init__()
        self.parent = parent
        self.options = kwargs
        self.created.append(self)

    def grid(self, **kwargs) -> None:
        self.grid_options = kwargs

    def columnconfigure(self, column, **kwargs) -> None:
        self.column_options = (column, kwargs)


def _config(backend: str, **models):
    return SimpleNamespace(
        summarizer_backend=backend,
        claude_code_model=models.get("claude_code_model", "haiku"),
        codex_model=models.get("codex_model", "gpt-5.6-luna"),
        gemini_model=models.get("gemini_model", "gemini-flash-latest"),
    )


def _bare_app(backend: str = "claude_code") -> App:
    app = App.__new__(App)
    app.config = _config(backend)
    app.backend_var = _Control(backend if backend in {"claude_code", "codex"} else "")
    app.backend_status_label = _Control()
    app.claude_backend_button = _Control()
    app.codex_backend_button = _Control()
    app._running = False
    app._append_status = lambda _message: None
    return app


def test_backend_controls_keep_both_login_buttons_and_segments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _Widget.created = []
    for name in ("Frame", "Label", "Radiobutton", "Button"):
        monkeypatch.setattr(app_module.tk, name, _Widget)
    app = _bare_app()

    app._build_backend_controls(_Widget())

    assert any(widget.options.get("text") == "요약 AI" for widget in _Widget.created)
    assert app.claude_login_button.options["text"] == "Claude 로그인"
    assert app.claude_login_button.options["command"] == app._on_claude_login
    assert app.codex_login_button.options["text"] == "Codex 로그인"
    assert app.codex_login_button.options["command"] == app._on_codex_login
    assert app.claude_backend_button.options["value"] == "claude_code"
    assert app.codex_backend_button.options["value"] == "codex"
    assert app.claude_backend_button.options["indicatoron"] is False
    assert app.codex_backend_button.options["indicatoron"] is False


def test_backend_switch_persists_and_replaces_config(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _bare_app("claude_code")
    replacement = _config("codex")
    app.backend_var.set("codex")
    monkeypatch.setattr(app_module, "save_summarizer_backend", lambda backend: replacement)

    app._on_backend_change()

    assert app.config is replacement
    assert app.backend_var.get() == "codex"
    assert "Codex" in app.backend_status_label.options["text"]


def test_backend_switch_failure_rolls_back_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _bare_app("claude_code")
    original = app.config
    app.backend_var.set("codex")
    errors = []

    def fail(_backend):
        raise ConfigError("저장 실패")

    monkeypatch.setattr(app_module, "save_summarizer_backend", fail)
    monkeypatch.setattr(app_module.messagebox, "showerror", lambda title, message: errors.append((title, message)))

    app._on_backend_change()

    assert app.config is original
    assert app.backend_var.get() == "claude_code"
    assert errors == [("요약 AI 변경 실패", "저장 실패")]


def test_backend_switch_is_blocked_while_recording(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _bare_app("claude_code")
    app._running = True
    app.backend_var.set("codex")
    warnings = []
    monkeypatch.setattr(
        app_module,
        "save_summarizer_backend",
        lambda _backend: pytest.fail("녹화 중에는 설정을 저장하면 안 됩니다"),
    )
    monkeypatch.setattr(app_module.messagebox, "showwarning", lambda title, message: warnings.append((title, message)))

    app._on_backend_change()

    assert app.backend_var.get() == "claude_code"
    assert warnings == [("변경할 수 없음", "녹화 중에는 요약 AI를 변경할 수 없습니다.")]


def test_api_backend_has_no_falsely_selected_cli_segment() -> None:
    app = _bare_app("gemini")

    app._sync_backend_controls()

    assert app.backend_var.get() == ""
    assert "Gemini" in app.backend_status_label.options["text"]


def test_running_state_disables_both_backend_segments() -> None:
    app = _bare_app("codex")
    app.url_entry = _Control()
    app.keywords_entry = _Control()
    app.start_begin_radio = _Control()
    app.start_offset_radio = _Control()
    app.start_time_entry = _Control()
    app.start_button = _Control()
    app.stop_button = _Control()
    app.reset_button = _Control()

    app._set_running_state(True)

    assert app._running is True
    assert app.claude_backend_button.options["state"] == "disabled"
    assert app.codex_backend_button.options["state"] == "disabled"


def test_on_start_passes_selected_config_to_session(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _bare_app("codex")
    chosen = app.config
    app.url_entry = _Control("https://youtube.test/live")
    app.keywords_entry = _Control("Python")
    app.start_mode = _Control("begin")
    started = []
    observed = {}

    class _Session:
        def __init__(self, config, url, keywords, callback, start_offset_sec):
            observed.update(config=config, url=url, keywords=keywords, offset=start_offset_sec)

        def start(self):
            started.append(True)

    monkeypatch.setattr(app_module, "Session", _Session)
    app._set_running_state = lambda running: observed.update(running=running)

    app._on_start()

    assert observed["config"] is chosen
    assert observed["url"] == "https://youtube.test/live"
    assert observed["running"] is True
    assert started == [True]


def test_login_handlers_remain_independent_of_selected_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _bare_app("codex")
    messages = []
    app._append_status = messages.append
    monkeypatch.setattr(app_module, "find_claude_cli", lambda: "claude")
    monkeypatch.setattr(app_module, "open_login_console", lambda: True)
    monkeypatch.setattr(app_module, "open_codex_login_console", lambda: None)

    app._on_claude_login()
    app.backend_var.set("claude_code")
    app._on_codex_login()

    assert any("Claude 로그인 창" in message for message in messages)
    assert any("Codex 로그인 창" in message for message in messages)
