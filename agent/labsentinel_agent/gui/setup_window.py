"""The window an admin sees when setting up a lab PC: type the code, done.

    labsentinel-setup.exe          (no console; asks for administrator rights)

Opened by the installer right after the files are copied, and from the Start
menu ("Pasangkan PC ini ke dashboard") to pair a PC again later. All the work
happens in :mod:`setup_flow` on a worker thread; this module only collects the
code, shows progress, and says plainly what happened.
"""

from __future__ import annotations

import contextlib
import logging
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk

from labsentinel_agent.gui.setup_flow import (
    ServiceControl,
    SetupFlow,
    SetupResult,
    default_service_control,
)

log = logging.getLogger("labsentinel.agent.setup.window")

# Same restrained palette as the dashboard (docs/adr/0005).
INK = "#15171c"
INK_2 = "#596070"
INK_3 = "#868d9b"
BORDER = "#e2e5ea"
BORDER_STRONG = "#cdd2da"
CANVAS = "#fbfbfc"
SURFACE = "#ffffff"
SUBTLE = "#f5f6f8"
ACCENT = "#2f5fd0"
ACCENT_HOVER = "#2850b5"
OK, OK_BG = "#15763c", "#e7f4ec"
DANGER, DANGER_BG = "#b3261e", "#fdecea"
INFO, INFO_BG = "#1e47a8", "#eaf0fd"

TRANSPARENCY = (
    "Setelah terpasang, PC ini dipantau hanya selama sesi lab. Peserta selalu "
    "melihat ikon Lab Sentinel di layar. Ketikan keyboard, suara, dan kamera "
    "tidak pernah direkam."
)


def _font_family() -> str:
    return "Segoe UI" if sys.platform == "win32" else "DejaVu Sans"


def _mono_family() -> str:
    return "Consolas" if sys.platform == "win32" else "DejaVu Sans Mono"


class SetupWindow:
    def __init__(self, root: tk.Tk, flow: SetupFlow) -> None:
        self.root = root
        self.flow = flow
        self.state = "idle"  # idle | busy | done | error | paired
        self._results: queue.Queue[SetupResult] = queue.Queue()
        self._style()
        self._build()
        if flow.already_paired():
            self._show_paired()
        else:
            self._show_form()

    # ------------------------------------------------------------- look
    def _style(self) -> None:
        f = _font_family()
        self.root.title("Lab Sentinel Agent — Pasang PC ini")
        self.root.configure(background=CANVAS)
        self.root.resizable(False, False)
        self.root.minsize(470, 1)  # same width in every state
        style = ttk.Style(self.root)
        with contextlib.suppress(tk.TclError):
            style.theme_use("clam")
        style.configure(".", font=(f, 10), background=CANVAS, foreground=INK)
        style.configure("Card.TFrame", background=SURFACE)
        style.configure("Head.TLabel", background=SURFACE, font=(f, 15, "bold"))
        style.configure("Sub.TLabel", background=SURFACE, foreground=INK_2, font=(f, 10))
        style.configure("Field.TLabel", background=CANVAS, font=(f, 10, "bold"))
        style.configure("Hint.TLabel", background=CANVAS, foreground=INK_2, font=(f, 9))
        style.configure("Note.TLabel", background=CANVAS, foreground=INK_3, font=(f, 9))
        style.configure("Link.TLabel", background=CANVAS, foreground=ACCENT, font=(f, 9))
        style.configure("Code.TEntry", padding=(10, 6), fieldbackground=SURFACE,
                        bordercolor=BORDER_STRONG, lightcolor=BORDER_STRONG,
                        darkcolor=BORDER_STRONG)
        style.map("Code.TEntry", bordercolor=[("focus", ACCENT)],
                  lightcolor=[("focus", ACCENT)], darkcolor=[("focus", ACCENT)])
        style.configure("Primary.TButton", background=ACCENT, foreground="#ffffff",
                        bordercolor=ACCENT, focuscolor=ACCENT, padding=(16, 6),
                        font=(f, 10, "bold"))
        style.map("Primary.TButton",
                  background=[("disabled", BORDER_STRONG), ("active", ACCENT_HOVER)],
                  foreground=[("disabled", "#ffffff")])
        style.configure("Plain.TButton", background=SURFACE, foreground=INK,
                        bordercolor=BORDER_STRONG, focuscolor=BORDER_STRONG, padding=(14, 6))
        style.map("Plain.TButton", background=[("active", SUBTLE)])
        style.configure("Busy.Horizontal.TProgressbar", troughcolor=SUBTLE,
                        background=ACCENT, bordercolor=SUBTLE, lightcolor=ACCENT,
                        darkcolor=ACCENT)

    def _build(self) -> None:
        f = _font_family()
        head = ttk.Frame(self.root, style="Card.TFrame", padding=(24, 18, 24, 16))
        head.pack(fill="x")
        ttk.Label(head, text="Lab Sentinel Agent", style="Head.TLabel").pack(anchor="w")
        ttk.Label(head, text="Hubungkan PC ini ke dashboard pengawas.",
                  style="Sub.TLabel").pack(anchor="w", pady=(2, 0))
        tk.Frame(self.root, height=1, background=BORDER).pack(fill="x")

        self.body = ttk.Frame(self.root, padding=(24, 18, 24, 6))
        self.body.pack(fill="both", expand=True)

        # Status box (hidden until there is something to say).
        self.status = tk.Frame(self.body, background=INFO_BG, highlightthickness=0)
        self.status_bar = tk.Frame(self.status, width=3, background=INFO)
        self.status_bar.pack(side="left", fill="y")
        inner = tk.Frame(self.status, background=INFO_BG)
        inner.pack(side="left", fill="both", expand=True, padx=12, pady=10)
        self.status_inner = inner
        self.status_title = tk.Label(inner, text="", anchor="w", justify="left",
                                     background=INFO_BG, foreground=INFO,
                                     font=(f, 10, "bold"))
        self.status_title.pack(fill="x")
        self.status_detail = tk.Label(inner, text="", anchor="w", justify="left",
                                      background=INFO_BG, foreground=INK,
                                      font=(f, 9), wraplength=380)
        self.status_detail.pack(fill="x", pady=(2, 0))
        self.progress = ttk.Progressbar(inner, mode="indeterminate", length=380,
                                        style="Busy.Horizontal.TProgressbar")

        # The form.
        self.form = ttk.Frame(self.body)
        ttk.Label(self.form, text="Kode pemasangan", style="Field.TLabel").pack(anchor="w")
        self.code_var = tk.StringVar()
        self.code = ttk.Entry(self.form, textvariable=self.code_var, width=12,
                              justify="center", style="Code.TEntry",
                              font=(_mono_family(), 22, "bold"))
        self.code.pack(anchor="w", pady=(6, 4))
        self.code_var.trace_add("write", lambda *_: self._sync_button())
        ttk.Label(self.form, text="Lihat di dashboard: halaman Aturan → Pemasangan PC baru.",
                  style="Hint.TLabel").pack(anchor="w")

        self.more = ttk.Label(self.form, text="▸ Alamat dashboard (opsional)",
                              style="Link.TLabel", cursor="hand2")
        self.more.pack(anchor="w", pady=(14, 0))
        self.more.bind("<Button-1>", lambda _e: self._toggle_address())
        self.address_box = ttk.Frame(self.form)
        self.address_var = tk.StringVar()
        self.address = ttk.Entry(self.address_box, textvariable=self.address_var, width=34)
        self.address.pack(anchor="w", pady=(6, 3))
        ttk.Label(self.address_box,
                  text="Kosongkan agar dashboard dicari otomatis di jaringan lab.\n"
                       "Isi bila lab berbeda jaringan, mis. 192.168.1.10",
                  style="Hint.TLabel", justify="left").pack(anchor="w")

        self.note = ttk.Label(self.body, text=TRANSPARENCY, style="Note.TLabel",
                              wraplength=400, justify="left")

        tk.Frame(self.root, height=1, background=BORDER).pack(fill="x", side="bottom")
        foot = ttk.Frame(self.root, padding=(24, 12))
        foot.pack(fill="x", side="bottom")
        self.primary = ttk.Button(foot, text="Pasangkan", style="Primary.TButton",
                                  command=self._on_primary)
        self.primary.pack(side="right")
        self.secondary = ttk.Button(foot, text="Batal", style="Plain.TButton",
                                    command=self._on_secondary)
        self.secondary.pack(side="right", padx=(0, 8))

        self.root.bind("<Return>", lambda _e: self._on_primary())
        self.root.bind("<Escape>", lambda _e: self._on_secondary())
        self.root.protocol("WM_DELETE_WINDOW", self._on_secondary)

    # ----------------------------------------------------------- states
    def _say(self, kind: str, title: str, detail: str) -> None:
        fg, bg = {"ok": (OK, OK_BG), "error": (DANGER, DANGER_BG)}.get(kind, (INFO, INFO_BG))
        icon = {"ok": "✓", "error": "⚠", "busy": "●"}.get(kind, "●")
        for frame in (self.status, self.status_inner):
            frame.configure(background=bg)
        for label in (self.status_title, self.status_detail):
            label.configure(background=bg)
        self.status_bar.configure(background=fg)
        self.status_title.configure(text=f"{icon}  {title}", foreground=fg)
        self.status_detail.configure(text=detail)
        if not self.status.winfo_ismapped():
            if self.form.winfo_ismapped():
                self.status.pack(fill="x", pady=(0, 16), before=self.form)
            else:
                self.status.pack(fill="x", pady=(0, 16), before=self.note)

    def _hide_status(self) -> None:
        self.progress.stop()
        self.progress.pack_forget()
        self.status.pack_forget()

    def _show_form(self) -> None:
        self.state = "idle"
        self._hide_status()
        self.form.pack(fill="x")
        self.note.pack(fill="x", pady=(18, 4))
        self.primary.configure(text="Pasangkan")
        self.secondary.configure(text="Batal")
        self._set_inputs(True)
        self._sync_button()
        self.code.focus_set()

    def _show_paired(self) -> None:
        self.state = "paired"
        running = self.flow.service_running()
        self.form.pack_forget()
        self.note.pack(fill="x", pady=(18, 4))
        self._say("ok" if running else "info", "PC ini sudah terpasang",
                  "Layanan pemantauan: " + ("berjalan." if running else
                                            "tidak berjalan. Pasangkan ulang untuk memulainya."))
        self.primary.configure(text="Pasangkan ulang", state="normal")
        self.secondary.configure(text="Tutup")

    def _set_inputs(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        self.code.configure(state=state)
        self.address.configure(state=state)

    def _sync_button(self) -> None:
        if self.state in ("idle", "error"):
            digits = sum(ch.isdigit() for ch in self.code_var.get())
            self.primary.configure(state="normal" if digits == 6 else "disabled")

    def _toggle_address(self) -> None:
        if self.address_box.winfo_ismapped():
            self.address_box.pack_forget()
            self.more.configure(text="▸ Alamat dashboard (opsional)")
        else:
            self.address_box.pack(anchor="w", fill="x")
            self.more.configure(text="▾ Alamat dashboard (opsional)")
            self.address.focus_set()

    # ---------------------------------------------------------- actions
    def _on_primary(self) -> None:
        if self.state == "paired":
            self._show_form()
        elif self.state == "done":
            self.root.destroy()
        elif self.state in ("idle", "error") and str(self.primary["state"]) != "disabled":
            self.submit()

    def _on_secondary(self) -> None:
        if self.state != "busy":
            self.root.destroy()

    def submit(self) -> None:
        self.state = "busy"
        self._set_inputs(False)
        self.primary.configure(state="disabled")
        self.secondary.configure(state="disabled")
        self._say("busy", "Menghubungi dashboard…",
                  "Mencari dashboard di jaringan lab dan memasangkan PC ini. "
                  "Biasanya hanya beberapa detik.")
        self.progress.pack(fill="x", pady=(8, 0))
        self.progress.start(12)
        code, address = self.code_var.get(), self.address_var.get()
        threading.Thread(target=self._work, args=(code, address), daemon=True).start()
        self.root.after(100, self._poll)

    def _work(self, code: str, address: str) -> None:
        try:
            result = self.flow.pair_and_start(code, address)
        except Exception as exc:  # never leave the window spinning
            log.exception("setup failed")
            result = SetupResult(False, "Terjadi kesalahan", type(exc).__name__, 1)
        self._results.put(result)

    def _poll(self) -> None:
        try:
            result = self._results.get_nowait()
        except queue.Empty:
            self.root.after(100, self._poll)
            return
        self.progress.stop()
        self.progress.pack_forget()
        self.secondary.configure(state="normal")
        self.last_result = result
        if result.ok:
            self.state = "done"
            self.form.pack_forget()
            self._say("ok", result.title, result.detail)
            self.primary.configure(text="Selesai", state="normal")
            self.secondary.pack_forget()
        else:
            self.state = "error"
            self._say("error", result.title, result.detail)
            self._set_inputs(True)
            self._sync_button()
            self.code.focus_set()
            self.code.select_range(0, "end")


# ------------------------------------------------------------------ main
def _is_admin() -> bool:
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _crisp_on_high_dpi() -> None:
    if sys.platform == "win32":
        with contextlib.suppress(Exception):
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)


def main(argv: list[str] | None = None) -> int:
    import argparse

    from labsentinel_agent.app import default_config_path
    from labsentinel_shared.config import load_agent_config
    from labsentinel_shared.logging_setup import setup_logging

    parser = argparse.ArgumentParser(prog="labsentinel-setup")
    parser.add_argument("--config", type=Path, default=None)
    args = parser.parse_args(argv)

    config_path = args.config or default_config_path()
    config = load_agent_config(config_path)
    with contextlib.suppress(OSError):
        setup_logging(log_path=config.data_dir / "logs" / "setup.log",
                      level=config.log_level, console=False)

    _crisp_on_high_dpi()
    root = tk.Tk()
    if not _is_admin():
        from tkinter import messagebox

        root.withdraw()
        messagebox.showerror(
            "Lab Sentinel Agent",
            "Pemasangan perlu hak administrator.\n\n"
            "Klik kanan program ini lalu pilih “Run as administrator”.",
        )
        return 1

    config_file = config_path or (
        Path(sys.executable).with_name("agent.toml") if getattr(sys, "frozen", False) else None
    )
    service: ServiceControl = default_service_control()
    window = SetupWindow(root, SetupFlow(config, service=service, config_file=config_file))
    root.update_idletasks()
    # Centre on screen.
    w, h = root.winfo_reqwidth(), root.winfo_reqheight()
    x = (root.winfo_screenwidth() - w) // 2
    y = (root.winfo_screenheight() - h) // 3
    root.geometry(f"+{max(0, x)}+{max(0, y)}")
    root.lift()
    root.attributes("-topmost", True)
    root.after(600, lambda: root.attributes("-topmost", False))
    root.mainloop()
    ok = window.state == "done"
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
