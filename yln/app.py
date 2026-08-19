"""Tkinter GUI for the YouTube Live -> Notion lecture note app."""

from __future__ import annotations

import datetime
import tkinter as tk
from tkinter import messagebox, scrolledtext
from typing import Optional

from yln.claude_cli import find_claude_cli, open_install_console, open_login_console, uninstall
from yln.config import Config, ConfigError, app_dir, load_config
from yln.session import Session, parse_start_time

WINDOW_TITLE = "YouTube Live → Notion 강의 노트"


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(WINDOW_TITLE)
        self.root.geometry("680x520")

        self.session: Optional[Session] = None
        self.config: Optional[Config] = None

        self._build_ui()

        try:
            self.config = load_config()
        except ConfigError as e:
            messagebox.showerror("설정 오류", str(e))

    def _build_ui(self) -> None:
        pad = {"padx": 8, "pady": 4}

        frame_top = tk.Frame(self.root)
        frame_top.pack(fill="x", **pad)
        frame_top.columnconfigure(1, weight=1)

        tk.Label(frame_top, text="유튜브 URL").grid(row=0, column=0, sticky="w")
        self.url_entry = tk.Entry(frame_top, width=60)
        self.url_entry.grid(row=0, column=1, sticky="we", padx=4, pady=2)

        tk.Label(frame_top, text="강의 주제 키워드").grid(row=1, column=0, sticky="w")
        self.keywords_entry = tk.Entry(frame_top, width=60)
        self.keywords_entry.grid(row=1, column=1, sticky="we", padx=4, pady=2)
        tk.Label(frame_top, text="예: Java, Spring, JWT", fg="gray").grid(row=2, column=1, sticky="w")

        tk.Label(frame_top, text="기록 시작").grid(row=3, column=0, sticky="w")
        start_frame = tk.Frame(frame_top)
        start_frame.grid(row=3, column=1, sticky="w", padx=4, pady=2)
        self.start_mode = tk.StringVar(value="begin")
        self.start_begin_radio = tk.Radiobutton(
            start_frame, text="처음부터", variable=self.start_mode, value="begin",
            command=self._on_start_mode_change,
        )
        self.start_begin_radio.pack(side="left")
        self.start_offset_radio = tk.Radiobutton(
            start_frame, text="지정 시간부터", variable=self.start_mode, value="offset",
            command=self._on_start_mode_change,
        )
        self.start_offset_radio.pack(side="left", padx=(12, 4))
        self.start_time_entry = tk.Entry(start_frame, width=10, state="disabled")
        self.start_time_entry.pack(side="left")
        tk.Label(
            frame_top, text="예: 1:40:00 — 녹화(업로드) 영상 전용, 이전 구간은 건너뜀", fg="gray"
        ).grid(row=4, column=1, sticky="w")

        frame_buttons = tk.Frame(self.root)
        frame_buttons.pack(fill="x", **pad)

        self.start_button = tk.Button(frame_buttons, text="시작", width=12, command=self._on_start)
        self.start_button.pack(side="left", padx=4)

        self.stop_button = tk.Button(frame_buttons, text="종료", width=12, command=self._on_stop, state="disabled")
        self.stop_button.pack(side="left", padx=4)

        self.reset_button = tk.Button(frame_buttons, text="초기화", width=12, command=self._on_reset)
        self.reset_button.pack(side="right", padx=4)

        self.claude_login_button = tk.Button(
            frame_buttons, text="Claude 로그인", width=14, command=self._on_claude_login
        )
        self.claude_login_button.pack(side="right", padx=4)

        self.counter_label = tk.Label(self.root, text="캡처된 슬라이드: 0")
        self.counter_label.pack(anchor="w", **pad)

        self.status_text = scrolledtext.ScrolledText(self.root, height=20, state="disabled", wrap="word")
        self.status_text.pack(fill="both", expand=True, **pad)

    # -- actions -----------------------------------------------------------

    def _on_start(self) -> None:
        if self.config is None:
            messagebox.showerror("설정 오류", "config.json 설정이 올바르지 않습니다. 앱을 다시 시작해주세요.")
            return

        url = self.url_entry.get().strip()
        if not url:
            messagebox.showwarning("입력 필요", "유튜브 URL을 입력해주세요.")
            return

        keywords = self.keywords_entry.get().strip()

        start_offset = 0.0
        if self.start_mode.get() == "offset":
            try:
                start_offset = parse_start_time(self.start_time_entry.get())
            except ValueError as e:
                messagebox.showwarning("입력 필요", str(e))
                return

        self.session = Session(self.config, url, keywords, self._on_status, start_offset_sec=start_offset)
        self._set_running_state(True)
        self._append_status("세션을 시작합니다...")
        self.session.start()

    def _on_start_mode_change(self) -> None:
        state = "normal" if self.start_mode.get() == "offset" else "disabled"
        self.start_time_entry.config(state=state)

    def _on_stop(self) -> None:
        if self.session is None:
            return
        if not messagebox.askyesno("종료 확인", "세션을 종료하시겠습니까? 마지막 슬라이드와 전체 요약이 기록됩니다."):
            return
        self.stop_button.config(state="disabled")
        self._append_status("종료 요청됨 — 마지막 슬라이드와 전체 요약을 기록하는 중...")
        self.session.stop()

    def _on_claude_login(self) -> None:
        if find_claude_cli() is None:
            if messagebox.askyesno(
                "Claude CLI 없음",
                "Claude CLI가 설치되어 있지 않습니다. 설치 창을 열까요? "
                "(설치 후 이 버튼을 다시 눌러 로그인하세요)",
            ):
                open_install_console()
                self._append_status("Claude CLI 설치 창을 열었습니다. 설치가 끝나면 'Claude 로그인' 버튼을 다시 눌러주세요.")
            return

        if open_login_console():
            self._append_status(
                "Claude 로그인 창을 열었습니다. 새로 열린 콘솔 창에서 '/login' 흐름에 따라 브라우저 인증을 완료해주세요."
            )
        else:
            messagebox.showerror("Claude 로그인 실패", "Claude CLI를 찾을 수 없어 로그인 창을 열지 못했습니다.")

    def _on_reset(self) -> None:
        if not messagebox.askyesno(
            "초기화 확인",
            "다음 항목을 모두 삭제합니다:\n\n"
            "- Claude CLI 로그인 정보 및 CLI 프로그램\n"
            "- ~/.claude 폴더 전체 (다른 Claude Code 사용 기록 포함)\n"
            "- 앱의 config.json (Notion 토큰·API 키)\n"
            "- 녹화 캐시\n\n"
            "Claude Code/Claude 데스크톱 앱을 사용 중이라면 먼저 종료하세요. "
            "이 작업은 되돌릴 수 없습니다.\n\n계속하시겠습니까?",
        ):
            return

        results = uninstall(app_dir() / "config.json")
        for line in results:
            self._append_status(line)
        self.config = None
        messagebox.showinfo(
            "초기화 완료", "초기화 완료 — 항목별 결과는 로그를 확인하세요. config.json이 삭제되어 앱 재설정이 필요합니다."
        )

    # -- status callback -----------------------------------------------------

    def _on_status(self, message: str) -> None:
        # Called from background threads; marshal onto the tkinter main thread.
        self.root.after(0, self._handle_status, message)

    def _handle_status(self, message: str) -> None:
        self._append_status(message)
        if self.session is not None:
            self.counter_label.config(text=f"캡처된 슬라이드: {self.session.slide_count}")
            if message.startswith("완료") or self.session.failed:
                self._set_running_state(False)

    def _append_status(self, message: str) -> None:
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.status_text.config(state="normal")
        self.status_text.insert("end", f"[{timestamp}] {message}\n")
        self.status_text.see("end")
        self.status_text.config(state="disabled")

    def _set_running_state(self, running: bool) -> None:
        input_state = "disabled" if running else "normal"
        self.url_entry.config(state=input_state)
        self.keywords_entry.config(state=input_state)
        self.start_begin_radio.config(state=input_state)
        self.start_offset_radio.config(state=input_state)
        if running:
            self.start_time_entry.config(state="disabled")
        else:
            self._on_start_mode_change()
        self.start_button.config(state="disabled" if running else "normal")
        self.stop_button.config(state="normal" if running else "disabled")
        self.reset_button.config(state="disabled" if running else "normal")


def main() -> None:
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
