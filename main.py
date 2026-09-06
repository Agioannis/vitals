"""
main.py — application shell.

Run:  python main.py            (add --interval 2 for an even lighter cadence)
On Windows, run the terminal as administrator to unlock temperatures and fan RPM.
"""

from __future__ import annotations

import argparse
import platform
import sys
import tkinter as tk

import customtkinter as ctk

from sensors import Sampler, is_admin
from ui import (C, FONTS, CpuPage, GpuPage, MemoryPage, NetworkPage, OverviewPage,
                StoragePage, SystemPage, band_temp, build_fonts)

IDLE_INTERVAL = 5.0  # seconds, while minimised


class HWMonitor(ctk.CTk):
    def __init__(self, interval: float = 1.0):
        super().__init__()
        self.title("Vitals — hardware monitor")
        self.geometry("1180x720")
        self.minsize(980, 600)
        self.configure(fg_color=C["bg"])
        build_fonts()

        self.interval = interval
        self.paused = False
        self._last_snap = None
        self._minimised = False

        self.sampler = Sampler(interval=interval)
        self.sampler.start()

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self._build_sidebar()
        self._build_header()
        self._build_pages()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Unmap>", self._on_unmap)
        self.bind("<Map>", self._on_map)
        self.after(300, self._tick)

    # -- chrome ------------------------------------------------------------- #

    def _build_sidebar(self):
        bar = ctk.CTkFrame(self, fg_color=C["panel"], corner_radius=0, width=196)
        bar.grid(row=0, column=0, rowspan=2, sticky="nsw")
        bar.grid_propagate(False)
        bar.grid_rowconfigure(9, weight=1)

        ctk.CTkLabel(bar, text="VITALS", font=FONTS["h1"], text_color=C["text"],
                     anchor="w").grid(row=0, column=0, sticky="ew", padx=18, pady=(20, 0))
        ctk.CTkLabel(bar, text="live hardware telemetry", font=FONTS["tiny"],
                     text_color=C["dim"], anchor="w").grid(row=1, column=0, sticky="ew",
                                                           padx=18, pady=(0, 16))
        self._nav_host = bar
        self._nav_buttons: dict[str, ctk.CTkButton] = {}

        ctrl = ctk.CTkFrame(bar, fg_color="transparent")
        ctrl.grid(row=10, column=0, sticky="ew", padx=14, pady=(0, 14))
        ctrl.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(ctrl, text="REFRESH EVERY", font=FONTS["eyebrow"],
                     text_color=C["dim"], anchor="w").grid(row=0, column=0, sticky="w")
        self._interval_menu = ctk.CTkOptionMenu(
            ctrl, values=["0.5 s", "1 s", "2 s", "5 s"], command=self._set_interval,
            fg_color=C["panel2"], button_color=C["panel2"], button_hover_color=C["line"],
            text_color=C["text"], font=FONTS["label"], dropdown_font=FONTS["label"],
            dropdown_fg_color=C["panel2"], height=30)
        self._interval_menu.set(f"{self.interval:g} s")
        self._interval_menu.grid(row=1, column=0, sticky="ew", pady=(4, 12))

        self._pause = ctk.CTkSwitch(ctrl, text="Pause polling", font=FONTS["label"],
                                    text_color=C["muted"], progress_color=C["accent"],
                                    command=self._toggle_pause)
        self._pause.grid(row=2, column=0, sticky="w")

        self.backend_var = tk.StringVar(value="")
        ctk.CTkLabel(ctrl, textvariable=self.backend_var, font=FONTS["tiny"],
                     text_color=C["dim"], anchor="w", wraplength=160,
                     justify="left").grid(row=3, column=0, sticky="w", pady=(12, 0))

    def _build_header(self):
        head = ctk.CTkFrame(self, fg_color="transparent", height=64)
        head.grid(row=0, column=1, sticky="ew", padx=18, pady=(16, 0))
        head.grid_columnconfigure(0, weight=1)

        self.page_title = tk.StringVar(value="Overview")
        ctk.CTkLabel(head, textvariable=self.page_title, font=FONTS["h1"],
                     text_color=C["text"], anchor="w").grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(head, text=platform.node(), font=FONTS["label"],
                     text_color=C["dim"], anchor="w").grid(row=1, column=0, sticky="w")

        self.state_var = tk.StringVar(value="—")
        self.state_pill = ctk.CTkLabel(head, textvariable=self.state_var, font=FONTS["numb"],
                                       text_color=C["bg"], fg_color=C["dim"],
                                       corner_radius=13, width=124, height=26)
        self.state_pill.grid(row=0, column=1, rowspan=2, sticky="e")

        if not is_admin() and sys.platform.startswith("win"):
            ctk.CTkLabel(head, text="running without administrator — some sensors hidden",
                         font=FONTS["tiny"], text_color=C["warm"], anchor="e"
                         ).grid(row=2, column=0, columnspan=2, sticky="e", pady=(4, 0))

    def _build_pages(self):
        host = ctk.CTkFrame(self, fg_color="transparent")
        host.grid(row=1, column=1, sticky="nsew", padx=12, pady=12)
        host.grid_columnconfigure(0, weight=1)
        host.grid_rowconfigure(0, weight=1)

        self.pages = {}
        classes = [OverviewPage, CpuPage, GpuPage, MemoryPage,
                   StoragePage, NetworkPage, SystemPage]
        for i, cls in enumerate(classes):
            page = cls(host, self)
            self.pages[cls.title] = page
            b = ctk.CTkButton(
                self._nav_host, text=cls.title, font=FONTS["nav"], anchor="w",
                height=34, corner_radius=8, fg_color="transparent",
                hover_color=C["panel2"], text_color=C["muted"],
                command=lambda t=cls.title: self.show(t))
            b.grid(row=2 + i, column=0, sticky="ew", padx=10, pady=1)
            self._nav_buttons[cls.title] = b

        self.current = "Overview"
        self.pages["Overview"].grid(row=0, column=0, sticky="nsew")
        self._style_nav()

    def _style_nav(self):
        for name, b in self._nav_buttons.items():
            on = name == self.current
            b.configure(fg_color=C["panel2"] if on else "transparent",
                        text_color=C["text"] if on else C["muted"])

    # -- behaviour ---------------------------------------------------------- #

    def show(self, name: str):
        if name == self.current:
            return
        self.pages[self.current].grid_forget()
        self.current = name
        self.pages[name].grid(row=0, column=0, sticky="nsew")
        self.page_title.set(name)
        self._style_nav()
        if self._last_snap is not None:
            self.pages[name].refresh(self._last_snap)

    def _set_interval(self, choice: str):
        self.interval = float(choice.split()[0])
        if not self._minimised:
            self.sampler.set_interval(self.interval)

    def _toggle_pause(self):
        self.paused = bool(self._pause.get())
        self.sampler.set_paused(self.paused)

    def _on_unmap(self, _e=None):
        if self.state() == "iconic" and not self._minimised:
            self._minimised = True
            self.sampler.set_interval(IDLE_INTERVAL)

    def _on_map(self, _e=None):
        if self._minimised:
            self._minimised = False
            self.sampler.set_interval(self.interval)

    def _tick(self):
        delay = int(self.interval * 1000)
        try:
            if self._minimised or self.paused:
                self.after(700, self._tick)
                return
            snap = self.sampler.snapshot()
            if snap is not None and snap is not self._last_snap:
                self._last_snap = snap
                self.pages[self.current].refresh(snap)
                self._update_state_pill(snap)
                if self.backend_var.get() != self.sampler.backend_note:
                    self.backend_var.set(self.sampler.backend_note)
        except Exception as exc:              # a bad sensor must not kill the UI
            print(f"[ui] refresh error: {exc}", file=sys.stderr)
        self.after(delay, self._tick)

    def _update_state_pill(self, snap):
        temps = [t for t in (snap.val("cpu.temp"), snap.val("gpu.temp")) if t is not None]
        if not temps:
            self.state_var.set("no thermals")
            self.state_pill.configure(fg_color=C["panel2"], text_color=C["dim"])
            return
        peak = max(temps)
        word = "cool" if peak < 45 else "nominal" if peak < 65 else "warm" if peak < 82 else "hot"
        self.state_var.set(f"{word}  ·  {peak:.0f} °C")
        self.state_pill.configure(fg_color=band_temp(peak), text_color=C["bg"])

    def _on_close(self):
        self.sampler.stop()
        self.sampler.join(timeout=2.0)
        self.destroy()


def parse_args():
    p = argparse.ArgumentParser(description="Vitals — lightweight hardware monitor")
    p.add_argument("--interval", type=float, default=1.0,
                   help="seconds between samples (default 1.0)")
    return p.parse_args()


def main():
    args = parse_args()
    ctk.set_appearance_mode("dark")
    app = HWMonitor(interval=max(args.interval, 0.25))
    app.mainloop()


if __name__ == "__main__":
    main()
