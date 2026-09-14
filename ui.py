"""
ui.py — the interface.

Design notes
------------
Chrome is neutral (graphite + periwinkle). Data is *thermal*: every number, bar
and sparkline colours itself by its own value band, so the panel visibly warms
up as the machine works. That ramp is the only place colour is spent.

Cost control
------------
* Widgets are built once; each tick only pushes text into StringVars and moves
  existing canvas items with coords() — no create/destroy in the hot path.
* Only the visible page is refreshed. Hidden pages cost nothing.
* Minimising the window drops the sampler to a 5 s idle cadence.
"""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from typing import Callable, Dict, List, Optional

import customtkinter as ctk

from sensors import Reading, Sampler, Snapshot

# --------------------------------------------------------------------------- #
# tokens
# --------------------------------------------------------------------------- #

C = {
    "bg":      "#0d1017",
    "panel":   "#151a23",
    "panel2":  "#1b212c",
    "line":    "#232b39",
    "text":    "#dfe4ec",
    "muted":   "#7d879c",
    "dim":     "#59627a",
    "accent":  "#7c8cf8",   # chrome only
    "cool":    "#4a9fd8",
    "ok":      "#3fbf9f",
    "warm":    "#e8a33d",
    "hot":     "#e05c5c",
}

PAD = 14


def band_temp(v: Optional[float]) -> str:
    if v is None:
        return C["dim"]
    return C["cool"] if v < 45 else C["ok"] if v < 65 else C["warm"] if v < 82 else C["hot"]


def band_load(v: Optional[float]) -> str:
    if v is None:
        return C["dim"]
    return C["cool"] if v < 25 else C["ok"] if v < 60 else C["warm"] if v < 85 else C["hot"]


def band_flat(_v=None) -> str:
    return C["accent"]


def blend(a: str, b: str, t: float) -> str:
    ai = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    bi = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{int(x + (y - x) * t):02x}" for x, y in zip(ai, bi))


FONTS: Dict[str, ctk.CTkFont] = {}


def build_fonts() -> None:
    fams = set(tkfont.families())

    def pick(*cands, fallback="TkDefaultFont"):
        for c in cands:
            if c in fams:
                return c
        return fallback

    ui = pick("Segoe UI", "Inter", "Ubuntu", "DejaVu Sans", "Helvetica")
    mono = pick("JetBrains Mono", "Cascadia Mono", "Consolas", "SF Mono",
                "DejaVu Sans Mono", "Courier New")
    FONTS.update({
        "eyebrow": ctk.CTkFont(ui, 10, "bold"),
        "label":   ctk.CTkFont(ui, 12),
        "labelb":  ctk.CTkFont(ui, 12, "bold"),
        "h1":      ctk.CTkFont(ui, 19, "bold"),
        "h2":      ctk.CTkFont(ui, 14, "bold"),
        "nav":     ctk.CTkFont(ui, 13),
        "hero":    ctk.CTkFont(mono, 34, "bold"),
        "big":     ctk.CTkFont(mono, 22, "bold"),
        "num":     ctk.CTkFont(mono, 12),
        "numb":    ctk.CTkFont(mono, 13, "bold"),
        "tiny":    ctk.CTkFont(mono, 10),
    })


# --------------------------------------------------------------------------- #
# formatting
# --------------------------------------------------------------------------- #

def fmt_rate(bps: Optional[float]) -> tuple[str, str]:
    if bps is None:
        return "—", "B/s"
    for unit, div in (("GB/s", 2 ** 30), ("MB/s", 2 ** 20), ("KB/s", 2 ** 10)):
        if bps >= div:
            return f"{bps / div:.2f}", unit
    return f"{bps:.0f}", "B/s"


def fmt_clock(mhz: Optional[float]) -> tuple[str, str]:
    if mhz is None:
        return "—", "MHz"
    return (f"{mhz / 1000:.2f}", "GHz") if mhz >= 1000 else (f"{mhz:.0f}", "MHz")


def fmt_uptime(sec: Optional[float]) -> str:
    if sec is None:
        return "—"
    s = int(sec)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m = s // 60
    return f"{d}d {h:02d}:{m:02d}" if d else f"{h:02d}:{m:02d}"


def fmt_reading(r: Optional[Reading]) -> str:
    if r is None or r.value is None:
        return "—"
    v, k = r.value, r.kind
    if k == "temp":
        return f"{v:.1f} °C"
    if k == "clock":
        return f"{v:,.0f} MHz"
    if k in ("load",):
        return f"{v:.1f} %"
    if k == "fan":
        return f"{v:,.0f} RPM"
    if k == "power":
        return f"{v:.1f} W"
    if k == "voltage":
        return f"{v:.3f} V"
    if k == "rate":
        a, b = fmt_rate(v)
        return f"{a} {b}"
    if k == "data":
        return f"{v:,.1f} {r.unit}"
    return f"{v:,.2f} {r.unit}".strip()


# --------------------------------------------------------------------------- #
# primitives
# --------------------------------------------------------------------------- #

class Sparkline(tk.Canvas):
    """Cheap history plot. Reuses two canvas items; no allocation per frame."""

    def __init__(self, master, height=54, bg=C["panel"],
                 band: Callable = band_flat, ymin=None, ymax=None, fill=True):
        super().__init__(master, height=height, bg=bg, highlightthickness=0, bd=0)
        self._band, self._ymin, self._ymax = band, ymin, ymax
        self._values: List[float] = []
        self._grid = [self.create_line(0, 0, 0, 0, fill=C["line"], width=1) for _ in range(3)]
        self._poly = self.create_polygon(0, 0, 0, 0, fill=bg, outline="") if fill else None
        self._line = self.create_line(0, 0, 0, 0, fill=C["accent"], width=2, joinstyle="round")
        self._dot = self.create_oval(0, 0, 0, 0, fill=C["accent"], outline="")
        self._bg = bg
        self.bind("<Configure>", lambda _e: self._draw())

    def set(self, values: List[float]) -> None:
        self._values = values
        self._draw()

    def _draw(self) -> None:
        w, h = self.winfo_width(), self.winfo_height()
        if w < 8 or h < 8:
            return
        pad = 4
        for i, gid in enumerate(self._grid):
            y = pad + (h - 2 * pad) * (i + 1) / 4
            self.coords(gid, 0, y, w, y)

        cap = max(int(w // 4), 30)   # fixed window, newest on the right (Task Manager style)
        vals = self._values[-cap:]
        if len(vals) < 2:
            return
        lo = self._ymin if self._ymin is not None else min(vals)
        hi = self._ymax if self._ymax is not None else max(vals)
        if hi - lo < 1e-6:                     # flat series sits mid-band, not on the floor
            lo, hi = lo - 0.5, hi + 0.5
        span = hi - lo
        step = w / (cap - 1)
        x0 = w - (len(vals) - 1) * step        # newest sample pinned to the right edge
        pts = []
        for i, v in enumerate(vals):
            x = x0 + i * step
            y = pad + (h - 2 * pad) * (1 - (v - lo) / span)
            pts.extend((x, y))

        col = self._band(vals[-1])
        self.coords(self._line, *pts)
        self.itemconfigure(self._line, fill=col)
        if self._poly is not None:
            self.coords(self._poly, *pts, pts[-2], h, pts[0], h)
            self.itemconfigure(self._poly, fill=blend(self._bg, col, 0.18))
        x, y = pts[-2], pts[-1]
        self.coords(self._dot, x - 3, y - 3, x + 3, y + 3)
        self.itemconfigure(self._dot, fill=col)
        self.tag_raise(self._line)
        self.tag_raise(self._dot)


class Meter(ctk.CTkFrame):
    """Label · bar · value, on one line."""

    def __init__(self, master, label: str, band: Callable = band_load, width_label=88):
        super().__init__(master, fg_color="transparent")
        self._band = band
        self.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(self, text=label, font=FONTS["label"], text_color=C["muted"],
                     width=width_label, anchor="w").grid(row=0, column=0, sticky="w")
        self._bar = ctk.CTkProgressBar(self, height=7, corner_radius=4,
                                       fg_color=C["line"], progress_color=C["accent"])
        self._bar.set(0)
        self._bar.grid(row=0, column=1, sticky="ew", padx=10)
        self._var = tk.StringVar(value="—")
        ctk.CTkLabel(self, textvariable=self._var, font=FONTS["num"],
                     text_color=C["text"], width=92, anchor="e").grid(row=0, column=2, sticky="e")

    def set(self, fraction: Optional[float], text: str, band_value: Optional[float] = None) -> None:
        self._bar.set(max(0.0, min(1.0, fraction or 0.0)))
        self._bar.configure(progress_color=self._band(
            band_value if band_value is not None else (fraction or 0) * 100))
        if self._var.get() != text:
            self._var.set(text)


class StatCard(ctk.CTkFrame):
    """Hero number + trend + up to three sub-readouts."""

    def __init__(self, master, title: str, unit: str,
                 band: Callable = band_load, subs: List[str] = (), ymin=None, ymax=None):
        super().__init__(master, fg_color=C["panel"], corner_radius=14)
        self._band = band
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=PAD, pady=(PAD, 0))
        head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(head, text=title.upper(), font=FONTS["eyebrow"],
                     text_color=C["muted"], anchor="w").grid(row=0, column=0, sticky="w")
        self._note = tk.StringVar(value="")
        ctk.CTkLabel(head, textvariable=self._note, font=FONTS["tiny"],
                     text_color=C["dim"], anchor="e").grid(row=0, column=1, sticky="e")

        val = ctk.CTkFrame(self, fg_color="transparent")
        val.grid(row=1, column=0, sticky="w", padx=PAD, pady=(2, 0))
        self._value = tk.StringVar(value="—")
        self._unit = tk.StringVar(value=unit)
        self._vlabel = ctk.CTkLabel(val, textvariable=self._value, font=FONTS["hero"],
                                    text_color=C["text"])
        self._vlabel.pack(side="left")
        ctk.CTkLabel(val, textvariable=self._unit, font=FONTS["label"],
                     text_color=C["muted"]).pack(side="left", padx=(6, 0), pady=(12, 0))

        self.spark = Sparkline(self, band=band, ymin=ymin, ymax=ymax)
        self.spark.grid(row=2, column=0, sticky="nsew", padx=PAD, pady=(6, 0))

        self._subs: Dict[str, tk.StringVar] = {}
        if subs:
            row = ctk.CTkFrame(self, fg_color="transparent")
            row.grid(row=3, column=0, sticky="ew", padx=PAD, pady=(8, PAD))
            for i, s in enumerate(subs):
                row.grid_columnconfigure(i, weight=1)
                cell = ctk.CTkFrame(row, fg_color="transparent")
                cell.grid(row=0, column=i, sticky="w")
                ctk.CTkLabel(cell, text=s.upper(), font=FONTS["eyebrow"],
                             text_color=C["dim"], anchor="w").pack(anchor="w")
                v = tk.StringVar(value="—")
                self._subs[s] = v
                ctk.CTkLabel(cell, textvariable=v, font=FONTS["numb"],
                             text_color=C["text"], anchor="w").pack(anchor="w")
        else:
            self.grid_rowconfigure(3, minsize=PAD)

    def update_card(self, value: Optional[float], text: str, unit: Optional[str] = None,
                    history: Optional[List[float]] = None, note: str = "",
                    subs: Optional[Dict[str, str]] = None) -> None:
        if self._value.get() != text:
            self._value.set(text)
        if unit is not None and self._unit.get() != unit:
            self._unit.set(unit)
        self._vlabel.configure(text_color=self._band(value) if value is not None else C["dim"])
        if self._note.get() != note:
            self._note.set(note)
        if history:
            self.spark.set(history)
        for k, v in (subs or {}).items():
            var = self._subs.get(k)
            if var is not None and var.get() != v:
                var.set(v)


class SensorTable(ctk.CTkScrollableFrame):
    """Every raw sensor, grouped by device. Rows are rebuilt only if the set changes."""

    def __init__(self, master, title="All sensors"):
        super().__init__(master, fg_color=C["panel"], corner_radius=14,
                         label_text=title.upper(), label_font=FONTS["eyebrow"],
                         label_fg_color=C["panel"], label_text_color=C["muted"],
                         label_anchor="w",
                         scrollbar_button_color=C["line"],
                         scrollbar_button_hover_color=C["dim"])
        self.grid_columnconfigure(0, weight=1)
        self._vars: Dict[str, tk.StringVar] = {}
        self._sig: Optional[tuple] = None

    def render(self, readings: List[Reading]) -> None:
        readings = [r for r in readings if r.value is not None]
        raw = [r for r in readings if "|" in r.key]   # straight from a backend
        readings = raw or readings
        readings.sort(key=lambda r: (r.source, r.kind, r.label))
        sig = tuple((r.source, r.key) for r in readings)
        if sig != self._sig:
            for w in self.winfo_children():
                w.destroy()
            self._vars.clear()
            self._sig = sig
            row = 0
            last_src = None
            for r in readings:
                if r.source != last_src:
                    last_src = r.source
                    ctk.CTkLabel(self, text=(r.source or "derived").upper(),
                                 font=FONTS["eyebrow"], text_color=C["accent"],
                                 anchor="w").grid(row=row, column=0, columnspan=2,
                                                  sticky="ew", padx=6, pady=(12, 2))
                    row += 1
                ctk.CTkLabel(self, text=r.label, font=FONTS["label"],
                             text_color=C["muted"], anchor="w"
                             ).grid(row=row, column=0, sticky="ew", padx=(6, 4), pady=1)
                var = tk.StringVar(value="—")
                self._vars[r.key] = var
                ctk.CTkLabel(self, textvariable=var, font=FONTS["num"],
                             text_color=C["text"], anchor="e", width=110
                             ).grid(row=row, column=1, sticky="e", padx=(4, 6), pady=1)
                row += 1
        for r in readings:
            var = self._vars.get(r.key)
            if var is not None:
                t = fmt_reading(r)
                if var.get() != t:
                    var.set(t)


def panel(master, title: str) -> ctk.CTkFrame:
    f = ctk.CTkFrame(master, fg_color=C["panel"], corner_radius=14)
    f.grid_columnconfigure(0, weight=1)
    ctk.CTkLabel(f, text=title.upper(), font=FONTS["eyebrow"], text_color=C["muted"],
                 anchor="w").grid(row=0, column=0, sticky="ew", padx=PAD, pady=(PAD, 6))
    return f


# --------------------------------------------------------------------------- #
# pages
# --------------------------------------------------------------------------- #

class Page(ctk.CTkFrame):
    title = "Page"

    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.build()

    def build(self) -> None: ...
    def refresh(self, s: Snapshot) -> None: ...

    def hist(self, key: str) -> List[float]:
        return self.app.sampler.history(key)


class OverviewPage(Page):
    title = "Overview"

    def build(self):
        for i in (0, 1):
            self.grid_columnconfigure(i, weight=1, uniform="ov")
            self.grid_rowconfigure(i, weight=1, uniform="ov")
        self.cpu = StatCard(self, "Processor", "%", band_load,
                            ["temp", "clock", "power"], ymin=0, ymax=100)
        self.gpu = StatCard(self, "Graphics", "%", band_load,
                            ["temp", "clock", "vram"], ymin=0, ymax=100)
        self.ram = StatCard(self, "Memory", "%", band_load,
                            ["used", "available", "swap"], ymin=0, ymax=100)
        self.net = StatCard(self, "Network", "MB/s", band_flat, ["up", "read", "write"])
        for w, (r, c) in zip((self.cpu, self.gpu, self.ram, self.net),
                             ((0, 0), (0, 1), (1, 0), (1, 1))):
            w.grid(row=r, column=c, sticky="nsew", padx=6, pady=6)

    def refresh(self, s: Snapshot):
        load = s.val("cpu.load")
        temp = s.val("cpu.temp")
        clk, cu = fmt_clock(s.val("cpu.clock"))
        pw = s.val("cpu.power")
        self.cpu.update_card(
            load, "—" if load is None else f"{load:.0f}", "%",
            self.hist("cpu.load"), s.meta.get("cpu.name", "")[:34],
            {"temp": "—" if temp is None else f"{temp:.0f} °C",
             "clock": f"{clk} {cu}",
             "power": "—" if pw is None else f"{pw:.0f} W"})

        gl, gt = s.val("gpu.load"), s.val("gpu.temp")
        gc, gu = fmt_clock(s.val("gpu.clock"))
        vp = s.val("vram.pct")
        self.gpu.update_card(
            gl, "—" if gl is None else f"{gl:.0f}", "%",
            self.hist("gpu.load"), s.meta.get("gpu.name", "no GPU sensors")[:34],
            {"temp": "—" if gt is None else f"{gt:.0f} °C",
             "clock": f"{gc} {gu}",
             "vram": "—" if vp is None else f"{vp:.0f} %"})

        rp = s.val("ram.pct")
        self.ram.update_card(
            rp, "—" if rp is None else f"{rp:.0f}", "%",
            self.hist("ram.pct"),
            f"{s.val('ram.total', 0):.1f} GB installed",
            {"used": f"{s.val('ram.used', 0):.1f} GB",
             "available": f"{s.val('ram.available', 0):.1f} GB",
             "swap": f"{s.val('swap.pct', 0):.0f} %"})

        dn, du = fmt_rate(s.val("net.down"))
        up, uu = fmt_rate(s.val("net.up"))
        rd, ru = fmt_rate(s.val("disk.read"))
        wr, wu = fmt_rate(s.val("disk.write"))
        self.net.update_card(None, dn, du, self.hist("net.down"), "download",
                             {"up": f"{up} {uu}", "read": f"{rd} {ru}", "write": f"{wr} {wu}"})


class CpuPage(Page):
    title = "CPU"

    def build(self):
        self.grid_columnconfigure(0, weight=3)
        self.grid_columnconfigure(1, weight=2)

        tiles = ctk.CTkFrame(self, fg_color="transparent")
        tiles.grid(row=0, column=0, columnspan=2, sticky="ew", padx=6, pady=(0, 6))
        specs = [("Load", "%", band_load, "cpu.load", (0, 100)),
                 ("Temperature", "°C", band_temp, "cpu.temp", (None, None)),
                 ("Clock", "GHz", band_flat, "cpu.clock", (None, None)),
                 ("Package power", "W", band_flat, "cpu.power", (None, None))]
        self.tiles = {}
        for i, (name, unit, band, key, (lo, hi)) in enumerate(specs):
            tiles.grid_columnconfigure(i, weight=1, uniform="t")
            card = StatCard(tiles, name, unit, band, ymin=lo, ymax=hi)
            card.configure(height=170)
            card.grid(row=0, column=i, sticky="nsew", padx=4)
            self.tiles[key] = card

        self.hint = ctk.CTkLabel(self, text="", font=FONTS["tiny"], text_color=C["dim"],
                                 anchor="w", wraplength=900, justify="left")
        self.hint.grid(row=1, column=0, columnspan=2, sticky="ew", padx=6)
        self.grid_rowconfigure(2, weight=1)

        self.cores_panel = panel(self, "Per-core load and clock")
        self.cores_panel.grid(row=2, column=0, sticky="nsew", padx=6, pady=6)
        self.cores_panel.grid_rowconfigure(1, weight=1)
        self.core_host = ctk.CTkScrollableFrame(
            self.cores_panel, fg_color="transparent",
            scrollbar_button_color=C["line"], scrollbar_button_hover_color=C["dim"])
        self.core_host.grid(row=1, column=0, sticky="nsew", padx=(PAD - 6), pady=(0, PAD))
        self.core_host.grid_columnconfigure(0, weight=1)
        self._core_meters: List[Meter] = []

        self.table = SensorTable(self, "CPU sensors")
        self.table.grid(row=2, column=1, sticky="nsew", padx=6, pady=6)

    def refresh(self, s: Snapshot):
        load = s.val("cpu.load")
        self.tiles["cpu.load"].update_card(
            load, "—" if load is None else f"{load:.0f}", "%",
            self.hist("cpu.load"), f"{len(s.cores)} thread" + ("s" if len(s.cores) != 1 else ""))
        t = s.val("cpu.temp")
        self.tiles["cpu.temp"].update_card(
            t, "—" if t is None else f"{t:.0f}", "°C", self.hist("cpu.temp"))
        v, u = fmt_clock(s.val("cpu.clock"))
        self.tiles["cpu.clock"].update_card(None, v, u, self.hist("cpu.clock"), "peak core")
        p = s.val("cpu.power")
        self.tiles["cpu.power"].update_card(
            None, "—" if p is None else f"{p:.0f}", "W", self.hist("cpu.power"))

        self.hint.configure(text="" if t is not None else
                            "CPU temperature, clock and power are blocked. On Windows this "
                            "usually means Memory Integrity (Core Isolation) is preventing "
                            "LibreHardwareMonitor's driver from reading CPU registers — GPU "
                            "and disk sensors are unaffected. Disable it in Windows Security "
                            "→ Device security → Core isolation and restart to unlock these.")

        if len(self._core_meters) != len(s.cores):
            for m in self._core_meters:
                m.destroy()
            self._core_meters = []
            for i in range(len(s.cores)):
                m = Meter(self.core_host, f"Thread {i}", band_load, width_label=68)
                m.grid(row=i, column=0, sticky="ew", pady=2)
                self._core_meters.append(m)
        for core, m in zip(s.cores, self._core_meters):
            clk = core.get("clock")
            extra = f"  {clk / 1000:.2f} GHz" if clk else ""
            m.set(core["load"] / 100.0, f"{core['load']:5.1f} %{extra}", core["load"])

        self.table.render(s.in_group("CPU"))


class GpuPage(Page):
    title = "GPU"

    def build(self):
        self.grid_columnconfigure(0, weight=3)
        self.grid_columnconfigure(1, weight=2)
        self.grid_rowconfigure(1, weight=1)

        tiles = ctk.CTkFrame(self, fg_color="transparent")
        tiles.grid(row=0, column=0, columnspan=2, sticky="ew", padx=6, pady=(0, 6))
        self.tiles = {}
        for i, (name, unit, band, key, rng) in enumerate([
                ("Load", "%", band_load, "gpu.load", (0, 100)),
                ("Temperature", "°C", band_temp, "gpu.temp", (None, None)),
                ("Core clock", "MHz", band_flat, "gpu.clock", (None, None)),
                ("Board power", "W", band_flat, "gpu.power", (None, None))]):
            tiles.grid_columnconfigure(i, weight=1, uniform="t")
            card = StatCard(tiles, name, unit, band, ymin=rng[0], ymax=rng[1])
            card.configure(height=170)
            card.grid(row=0, column=i, sticky="nsew", padx=4)
            self.tiles[key] = card

        left = panel(self, "Memory and cooling")
        left.grid(row=1, column=0, sticky="nsew", padx=6, pady=6)
        self.vram = Meter(left, "VRAM", band_load)
        self.vram.grid(row=1, column=0, sticky="ew", padx=PAD, pady=4)
        self.fan = Meter(left, "Fan", band_load)
        self.fan.grid(row=2, column=0, sticky="ew", padx=PAD, pady=4)
        self.hint = ctk.CTkLabel(left, text="", font=FONTS["label"], text_color=C["dim"],
                                 wraplength=380, justify="left")
        self.hint.grid(row=3, column=0, sticky="w", padx=PAD, pady=(10, PAD))

        self.table = SensorTable(self, "GPU sensors")
        self.table.grid(row=1, column=1, sticky="nsew", padx=6, pady=6)

    def refresh(self, s: Snapshot):
        gl = s.val("gpu.load")
        self.tiles["gpu.load"].update_card(
            gl, "—" if gl is None else f"{gl:.0f}", "%", self.hist("gpu.load"),
            s.meta.get("gpu.name", "")[:30])
        gt = s.val("gpu.temp")
        self.tiles["gpu.temp"].update_card(
            gt, "—" if gt is None else f"{gt:.0f}", "°C", self.hist("gpu.temp"))
        v, u = fmt_clock(s.val("gpu.clock"))
        self.tiles["gpu.clock"].update_card(None, v, u, self.hist("gpu.clock"))
        gp = s.val("gpu.power")
        self.tiles["gpu.power"].update_card(
            None, "—" if gp is None else f"{gp:.0f}", "W", self.hist("gpu.power"))

        used, total, pct = s.val("vram.used"), s.val("vram.total"), s.val("vram.pct")
        if total:
            self.vram.set((pct or 0) / 100, f"{used:,.0f} / {total:,.0f} MB", pct)
        else:
            self.vram.set(0, "—")
        fan = s.val("gpu.fan")
        self.fan.set(min((fan or 0) / 3000, 1.0), "—" if fan is None else f"{fan:,.0f} RPM",
                     None if fan is None else fan / 30)

        readings = s.in_group("GPU")
        self.hint.configure(text="" if readings else
                            "No GPU sensors reported. On Windows this usually means "
                            "LibreHardwareMonitorLib.dll is missing, or the app is not "
                            "running as administrator.")
        self.table.render(readings)


class MemoryPage(Page):
    title = "Memory"

    def build(self):
        self.grid_columnconfigure(0, weight=3)
        self.grid_columnconfigure(1, weight=2)
        self.grid_rowconfigure(1, weight=1)

        top = ctk.CTkFrame(self, fg_color="transparent")
        top.grid(row=0, column=0, columnspan=2, sticky="ew", padx=6, pady=(0, 6))
        top.grid_columnconfigure(0, weight=1)
        self.card = StatCard(top, "Physical memory", "%", band_load,
                             ["used", "available", "total"], ymin=0, ymax=100)
        self.card.configure(height=200)
        self.card.grid(row=0, column=0, sticky="nsew")

        left = panel(self, "Allocation")
        left.grid(row=1, column=0, sticky="nsew", padx=6, pady=6)
        self.m_ram = Meter(left, "RAM")
        self.m_ram.grid(row=1, column=0, sticky="ew", padx=PAD, pady=4)
        self.m_swap = Meter(left, "Swap / pagefile")
        self.m_swap.grid(row=2, column=0, sticky="ew", padx=PAD, pady=(4, PAD))

        self.procs = panel(self, "Heaviest processes by memory")
        self.procs.grid(row=2, column=0, columnspan=2, sticky="nsew", padx=6, pady=6)
        self.grid_rowconfigure(2, weight=1)
        self.proc_rows: List[tuple] = []
        for i in range(8):
            name = tk.StringVar(value="")
            mem = tk.StringVar(value="")
            ctk.CTkLabel(self.procs, textvariable=name, font=FONTS["label"],
                         text_color=C["muted"], anchor="w"
                         ).grid(row=i + 1, column=0, sticky="ew", padx=(PAD, 4), pady=1)
            ctk.CTkLabel(self.procs, textvariable=mem, font=FONTS["num"],
                         text_color=C["text"], anchor="e", width=110
                         ).grid(row=i + 1, column=1, sticky="e", padx=(4, PAD), pady=1)
            self.proc_rows.append((name, mem))

        self.table = SensorTable(self, "Memory sensors")
        self.table.grid(row=1, column=1, sticky="nsew", padx=6, pady=6)

    def refresh(self, s: Snapshot):
        pct = s.val("ram.pct")
        self.card.update_card(
            pct, "—" if pct is None else f"{pct:.0f}", "%", self.hist("ram.pct"), "",
            {"used": f"{s.val('ram.used', 0):.2f} GB",
             "available": f"{s.val('ram.available', 0):.2f} GB",
             "total": f"{s.val('ram.total', 0):.2f} GB"})
        self.m_ram.set((pct or 0) / 100,
                       f"{s.val('ram.used', 0):.1f} / {s.val('ram.total', 0):.1f} GB", pct)
        sp = s.val("swap.pct", 0)
        self.m_swap.set(sp / 100,
                        f"{s.val('swap.used', 0):.1f} / {s.val('swap.total', 0):.1f} GB", sp)
        for (nvar, mvar), p in zip(self.proc_rows, s.procs + [None] * 8):
            if p is None:
                nvar.set(""); mvar.set("")
            else:
                nvar.set(f"{p['name']}  ·  pid {p['pid']}")
                mvar.set(f"{p['rss']:,.0f} MB")
        self.table.render(s.in_group("Memory"))


class StoragePage(Page):
    title = "Storage"

    def build(self):
        self.grid_columnconfigure(0, weight=3)
        self.grid_columnconfigure(1, weight=2)
        self.grid_rowconfigure(1, weight=1)

        top = ctk.CTkFrame(self, fg_color="transparent")
        top.grid(row=0, column=0, columnspan=2, sticky="ew", padx=6, pady=(0, 6))
        for i in (0, 1, 2):
            top.grid_columnconfigure(i, weight=1, uniform="t")
        self.read = StatCard(top, "Read", "MB/s", band_flat)
        self.write = StatCard(top, "Write", "MB/s", band_flat)
        self.temp = StatCard(top, "Hottest drive", "°C", band_temp)
        for i, c in enumerate((self.read, self.write, self.temp)):
            c.configure(height=170)
            c.grid(row=0, column=i, sticky="nsew", padx=4)

        self.vols = panel(self, "Volumes")
        self.vols.grid(row=1, column=0, sticky="nsew", padx=6, pady=6)
        self._vol_meters: Dict[str, Meter] = {}

        self.table = SensorTable(self, "Drive sensors")
        self.table.grid(row=1, column=1, sticky="nsew", padx=6, pady=6)

    def refresh(self, s: Snapshot):
        a, u = fmt_rate(s.val("disk.read"))
        self.read.update_card(None, a, u, self.hist("disk.read"))
        a, u = fmt_rate(s.val("disk.write"))
        self.write.update_card(None, a, u, self.hist("disk.write"))
        t = s.val("disk.temp")
        self.temp.update_card(t, "—" if t is None else f"{t:.0f}", "°C", None,
                              (s.get("disk.temp").source[:26] if s.get("disk.temp") else ""))

        keys = {p["mount"] for p in s.partitions}
        if keys != set(self._vol_meters):
            for m in self._vol_meters.values():
                m.destroy()
            self._vol_meters = {}
            for i, p in enumerate(s.partitions):
                m = Meter(self.vols, p["mount"][:16], band_load, width_label=120)
                m.grid(row=i + 1, column=0, sticky="ew", padx=PAD, pady=3)
                self._vol_meters[p["mount"]] = m
        for p in s.partitions:
            m = self._vol_meters.get(p["mount"])
            if m:
                m.set(p["pct"] / 100, f"{p['used']:,.0f} / {p['total']:,.0f} GB", p["pct"])

        self.table.render(s.in_group("Storage"))


class NetworkPage(Page):
    title = "Network"

    def build(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self.down = StatCard(self, "Download", "MB/s", band_flat)
        self.up = StatCard(self, "Upload", "MB/s", band_flat)
        self.down.configure(height=200)
        self.up.configure(height=200)
        self.down.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)
        self.up.grid(row=0, column=1, sticky="nsew", padx=6, pady=6)

        self.ifaces = panel(self, "Interfaces · session totals")
        self.ifaces.grid(row=1, column=0, columnspan=2, sticky="nsew", padx=6, pady=6)
        self._rows: Dict[str, tuple] = {}

    def refresh(self, s: Snapshot):
        a, u = fmt_rate(s.val("net.down"))
        self.down.update_card(None, a, u, self.hist("net.down"))
        a, u = fmt_rate(s.val("net.up"))
        self.up.update_card(None, a, u, self.hist("net.up"))

        names = [n["name"] for n in s.nics[:8]]
        if set(names) != set(self._rows):
            for ws in self._rows.values():
                for w in ws[2:]:
                    w.destroy()
            self._rows = {}
            for i, name in enumerate(names):
                v1, v2 = tk.StringVar(), tk.StringVar()
                l0 = ctk.CTkLabel(self.ifaces, text=name[:28], font=FONTS["label"],
                                  text_color=C["muted"], anchor="w")
                l0.grid(row=i + 1, column=0, sticky="ew", padx=(PAD, 4), pady=2)
                l1 = ctk.CTkLabel(self.ifaces, textvariable=v1, font=FONTS["num"],
                                  text_color=C["text"], anchor="e", width=170)
                l1.grid(row=i + 1, column=1, sticky="e", pady=2)
                l2 = ctk.CTkLabel(self.ifaces, textvariable=v2, font=FONTS["num"],
                                  text_color=C["dim"], anchor="e", width=190)
                l2.grid(row=i + 1, column=2, sticky="e", padx=(4, PAD), pady=2)
                self._rows[name] = (v1, v2, l0, l1, l2)
        for n in s.nics[:8]:
            r = self._rows.get(n["name"])
            if not r:
                continue
            d, du = fmt_rate(n["down"])
            up, uu = fmt_rate(n["up"])
            r[0].set(f"↓ {d} {du}   ↑ {up} {uu}")
            r[1].set(f"{n['rx'] / 2**30:,.2f} GB in   {n['tx'] / 2**30:,.2f} GB out")


class SystemPage(Page):
    title = "System"

    def build(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)

        info = panel(self, "Machine")
        info.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)
        self._info: Dict[str, tk.StringVar] = {}
        rows = ["Processor", "Graphics", "Motherboard", "Operating system",
                "Python", "Uptime", "Processes", "Sensor backend", "CPU affinity"]
        for i, name in enumerate(rows):
            ctk.CTkLabel(info, text=name, font=FONTS["label"], text_color=C["muted"],
                         anchor="w", width=140).grid(row=i + 1, column=0, sticky="w",
                                                     padx=(PAD, 4), pady=2)
            v = tk.StringVar(value="—")
            self._info[name] = v
            ctk.CTkLabel(info, textvariable=v, font=FONTS["num"], text_color=C["text"],
                         anchor="w", wraplength=320, justify="left"
                         ).grid(row=i + 1, column=1, sticky="w", padx=(4, PAD), pady=2)

        cost = panel(self, "This monitor's own footprint")
        cost.grid(row=0, column=1, sticky="nsew", padx=6, pady=6)
        self.self_cpu = Meter(cost, "CPU", band_load)
        self.self_cpu.grid(row=1, column=0, sticky="ew", padx=PAD, pady=4)
        self.self_ram = Meter(cost, "Memory", band_load)
        self.self_ram.grid(row=2, column=0, sticky="ew", padx=PAD, pady=4)
        ctk.CTkLabel(cost, text="Raise the refresh interval in the sidebar to lower this. "
                               "Minimising the window drops polling to once every 5 s.",
                     font=FONTS["label"], text_color=C["dim"], wraplength=340,
                     justify="left").grid(row=3, column=0, sticky="w", padx=PAD, pady=(8, PAD))

        self.table = SensorTable(self, "Board, chipset and other sensors")
        self.table.grid(row=1, column=0, columnspan=2, sticky="nsew", padx=6, pady=6)

    def refresh(self, s: Snapshot):
        import platform
        import sys as _sys
        vals = {
            "Processor": s.meta.get("cpu.name", "—"),
            "Graphics": s.meta.get("gpu.name", "—"),
            "Motherboard": s.meta.get("board.name", "—"),
            "Operating system": f"{platform.system()} {platform.release()} ({platform.machine()})",
            "Python": _sys.version.split()[0],
            "Uptime": fmt_uptime(s.val("sys.uptime")),
            "Processes": s.meta.get("proc.count", "—"),
            "Sensor backend": self.app.sampler.backend_note,
            "CPU affinity": (f"pinned to core {self.app.pinned_core} (least busy at launch)"
                             if self.app.pinned_core is not None else "not pinned"),
        }
        for k, v in vals.items():
            if self._info[k].get() != v:
                self._info[k].set(v)
        c = s.val("self.cpu", 0.0)
        r = s.val("self.ram", 0.0)
        self.self_cpu.set(min(c / 5.0, 1.0), f"{c:.2f} % of total", c * 10)
        self.self_ram.set(min(r / 300.0, 1.0), f"{r:,.0f} MB", r / 3)
        self.table.render(s.in_group("System"))
