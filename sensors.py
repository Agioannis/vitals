"""
sensors.py — hardware data collection layer.

Two backends are merged into one snapshot:

  * psutil                 — always available. Load, memory, disks, network, uptime.
  * LibreHardwareMonitor   — Windows only, optional. Real temperatures, per-core
                             clocks, fan RPM, package power, GPU/VRAM, drive temps.

Everything runs on ONE background thread on a two-tier schedule so the UI thread
never blocks and cheap counters are not re-read at the price of expensive ones.
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Dict, List, Optional

import psutil

IS_WIN = sys.platform.startswith("win")
IS_LINUX = sys.platform.startswith("linux")

HISTORY_LEN = 240  # points kept per metric (~4 min at 1 s)


# --------------------------------------------------------------------------- #
# data model
# --------------------------------------------------------------------------- #

@dataclass(slots=True)
class Reading:
    key: str
    label: str
    value: Optional[float]
    unit: str = ""
    kind: str = "misc"      # temp | clock | load | fan | power | voltage | data | rate
    group: str = "System"   # CPU | GPU | Memory | Storage | Network | System
    source: str = ""        # hardware name the sensor came from


class Snapshot(dict):
    """key -> Reading, plus a few structured extras."""

    def __init__(self):
        super().__init__()
        self.ts = time.time()
        self.meta: Dict[str, str] = {}
        self.cores: List[dict] = []
        self.partitions: List[dict] = []
        self.nics: List[dict] = []
        self.procs: List[dict] = []

    def add(self, r: Reading) -> None:
        self[r.key] = r

    def put(self, key, label, value, unit="", kind="misc", group="System", source=""):
        self[key] = Reading(key, label, value, unit, kind, group, source)

    def val(self, key, default=None):
        r = self.get(key)
        if r is None or r.value is None:
            return default
        return r.value

    def in_group(self, group: str) -> List[Reading]:
        return [r for r in self.values() if r.group == group and not r.key.startswith("_")]


def is_admin() -> bool:
    if IS_WIN:
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    try:
        return os.geteuid() == 0
    except AttributeError:
        return False


# --------------------------------------------------------------------------- #
# LibreHardwareMonitor backend (Windows)
# --------------------------------------------------------------------------- #

_SENSOR_UNITS = {
    "Temperature": ("°C", "temp"),
    "Clock": ("MHz", "clock"),
    "Load": ("%", "load"),
    "Fan": ("RPM", "fan"),
    "Power": ("W", "power"),
    "Voltage": ("V", "voltage"),
    "Current": ("A", "misc"),
    "Data": ("GB", "data"),
    "SmallData": ("MB", "data"),
    "Throughput": ("B/s", "rate"),
    "Control": ("%", "load"),
    "Level": ("%", "load"),
    "Frequency": ("Hz", "clock"),
    "Factor": ("", "misc"),
    "Energy": ("mWh", "misc"),
    "Noise": ("dBA", "misc"),
}

_HW_GROUP = {
    "Cpu": "CPU",
    "GpuNvidia": "GPU",
    "GpuAmd": "GPU",
    "GpuIntel": "GPU",
    "Memory": "Memory",
    "Storage": "Storage",
    "Network": "Network",
    "Motherboard": "System",
    "SuperIO": "System",
    "Battery": "System",
    "Cooler": "System",
    "Psu": "System",
    "EmbeddedController": "System",
}


class LibreHardwareBackend:
    """Thin wrapper over LibreHardwareMonitorLib.dll via pythonnet."""

    name = "LibreHardwareMonitor"

    def __init__(self, dll_path: Optional[str] = None):
        import clr  # pythonnet

        path = dll_path or self._locate()
        if path is None:
            raise FileNotFoundError(
                "LibreHardwareMonitorLib.dll not found. Drop it (and HidSharp.dll) "
                "into the lib/ folder next to sensors.py, or set the LHM_DLL env var."
            )
        folder = os.path.dirname(os.path.abspath(path))
        if folder not in sys.path:
            sys.path.append(folder)
        clr.AddReference(os.path.splitext(os.path.abspath(path))[0])

        from LibreHardwareMonitor import Hardware  # type: ignore

        c = Hardware.Computer()
        c.IsCpuEnabled = True
        c.IsGpuEnabled = True
        c.IsMemoryEnabled = True
        c.IsMotherboardEnabled = True
        c.IsStorageEnabled = True
        c.IsControllerEnabled = True
        c.IsBatteryEnabled = True
        c.IsPsuEnabled = True
        c.IsNetworkEnabled = False  # psutil already covers this, and cheaper
        c.Open()
        self._computer = c

    @staticmethod
    def _locate() -> Optional[str]:
        roots = [os.path.dirname(os.path.abspath(__file__))]
        # frozen by PyInstaller: bundled data lives in the unpack dir, and the
        # user may also have dropped the DLL next to the .exe itself
        bundle = getattr(sys, "_MEIPASS", None)
        if bundle:
            roots.insert(0, bundle)
            roots.append(os.path.dirname(sys.executable))
        candidates = [os.environ.get("LHM_DLL", "")]
        for root in roots:
            candidates.append(os.path.join(root, "lib", "LibreHardwareMonitorLib.dll"))
            candidates.append(os.path.join(root, "LibreHardwareMonitorLib.dll"))
        candidates.append(r"C:\Program Files\LibreHardwareMonitor\LibreHardwareMonitorLib.dll")
        for c in candidates:
            if c and os.path.isfile(c):
                return c
        return None

    def read(self, snap: Snapshot) -> None:
        for hw in self._computer.Hardware:
            self._read_hw(hw, snap)

    def _read_hw(self, hw, snap: Snapshot) -> None:
        hw.Update()
        group = _HW_GROUP.get(str(hw.HardwareType), "System")
        hwname = str(hw.Name)

        if group == "CPU" and "cpu.name" not in snap.meta:
            snap.meta["cpu.name"] = hwname
        elif group == "GPU" and "gpu.name" not in snap.meta:
            snap.meta["gpu.name"] = hwname
        elif str(hw.HardwareType) == "Motherboard":
            snap.meta.setdefault("board.name", hwname)

        for s in hw.Sensors:
            self._add(snap, hwname, group, s)
        for sub in hw.SubHardware:
            sub.Update()
            for s in sub.Sensors:
                self._add(snap, f"{hwname} · {sub.Name}", group, s)

    @staticmethod
    def _add(snap: Snapshot, hwname: str, group: str, s) -> None:
        stype = str(s.SensorType)
        unit, kind = _SENSOR_UNITS.get(stype, ("", "misc"))
        try:
            v = s.Value
            v = None if v is None else float(v)
        except Exception:
            v = None
        key = f"lhm|{hwname}|{stype}|{s.Name}"
        snap.add(Reading(key, str(s.Name), v, unit, kind, group, hwname))

    def close(self) -> None:
        try:
            self._computer.Close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# psutil backend (all platforms)
# --------------------------------------------------------------------------- #

class PsutilBackend:
    name = "psutil"

    def __init__(self):
        psutil.cpu_percent(percpu=True)  # prime the delta counters
        self._net = psutil.net_io_counters(pernic=True)
        self._disk = psutil.disk_io_counters()
        self._t = time.monotonic()
        self._slow_cache = Snapshot()
        self._slow_extras = {"partitions": [], "procs": [], "nics_static": []}

    # -- fast tier ---------------------------------------------------------- #

    def read_fast(self, snap: Snapshot) -> None:
        now = time.monotonic()
        dt = max(now - self._t, 1e-3)
        self._t = now

        per_core = psutil.cpu_percent(percpu=True)
        snap.put("cpu.load", "CPU load", sum(per_core) / len(per_core), "%", "load", "CPU")

        freqs = []
        try:
            f = psutil.cpu_freq(percpu=True)
            freqs = [x.current for x in f] if f else []
            if not freqs:
                one = psutil.cpu_freq()
                if one:
                    freqs = [one.current] * len(per_core)
        except Exception:
            pass
        if freqs:
            snap.put("cpu.clock", "CPU clock", max(freqs), "MHz", "clock", "CPU")

        snap.cores = [
            {"i": i, "load": per_core[i], "clock": freqs[i] if i < len(freqs) else None}
            for i in range(len(per_core))
        ]

        vm = psutil.virtual_memory()
        snap.put("ram.used", "Memory used", vm.used / 2**30, "GB", "data", "Memory")
        snap.put("ram.total", "Memory total", vm.total / 2**30, "GB", "data", "Memory")
        snap.put("ram.pct", "Memory usage", vm.percent, "%", "load", "Memory")
        snap.put("ram.available", "Memory available", vm.available / 2**30, "GB", "data", "Memory")

        sw = psutil.swap_memory()
        snap.put("swap.pct", "Swap usage", sw.percent, "%", "load", "Memory")
        snap.put("swap.used", "Swap used", sw.used / 2**30, "GB", "data", "Memory")
        snap.put("swap.total", "Swap total", sw.total / 2**30, "GB", "data", "Memory")

        # network throughput
        nics = psutil.net_io_counters(pernic=True)
        down = up = 0.0
        rows = []
        for name, c in nics.items():
            prev = self._net.get(name)
            d = (c.bytes_recv - prev.bytes_recv) / dt if prev else 0.0
            u = (c.bytes_sent - prev.bytes_sent) / dt if prev else 0.0
            d, u = max(d, 0.0), max(u, 0.0)
            down += d
            up += u
            rows.append({"name": name, "down": d, "up": u,
                         "rx": c.bytes_recv, "tx": c.bytes_sent})
        self._net = nics
        snap.nics = sorted(rows, key=lambda r: r["rx"] + r["tx"], reverse=True)
        snap.put("net.down", "Download", down, "B/s", "rate", "Network")
        snap.put("net.up", "Upload", up, "B/s", "rate", "Network")

        # disk throughput
        try:
            dio = psutil.disk_io_counters()
            if dio and self._disk:
                snap.put("disk.read", "Disk read",
                         max((dio.read_bytes - self._disk.read_bytes) / dt, 0.0),
                         "B/s", "rate", "Storage")
                snap.put("disk.write", "Disk write",
                         max((dio.write_bytes - self._disk.write_bytes) / dt, 0.0),
                         "B/s", "rate", "Storage")
            self._disk = dio
        except Exception:
            pass

        # native sensors (Linux; also some Windows laptops via WMI, rarely)
        try:
            temps = psutil.sensors_temperatures()
        except Exception:
            temps = {}
        for chip, entries in (temps or {}).items():
            for e in entries:
                if e.current is None:
                    continue
                grp = "CPU" if chip in ("coretemp", "k10temp", "zenpower", "cpu_thermal") else "System"
                label = e.label or chip
                snap.add(Reading(f"ps|{chip}|{label}", label, float(e.current),
                                 "°C", "temp", grp, chip))
        try:
            fans = psutil.sensors_fans() or {}
        except Exception:
            fans = {}
        for chip, entries in fans.items():
            for e in entries:
                label = e.label or chip
                snap.add(Reading(f"psfan|{chip}|{label}", label, float(e.current),
                                 "RPM", "fan", "System", chip))

        try:
            b = psutil.sensors_battery()
            if b:
                snap.put("battery.pct", "Battery", b.percent, "%", "load", "System")
                snap.meta["battery.state"] = "charging" if b.power_plugged else "on battery"
        except Exception:
            pass

        snap.put("sys.uptime", "Uptime", time.time() - psutil.boot_time(), "s", "misc", "System")

        # this app's own footprint — keeps us honest about "lightweight"
        try:
            me = psutil.Process()
            snap.put("self.cpu", "Monitor CPU", me.cpu_percent() / psutil.cpu_count(), "%", "load", "System")
            snap.put("self.ram", "Monitor RAM", me.memory_info().rss / 2**20, "MB", "data", "System")
        except Exception:
            pass

    # -- slow tier ---------------------------------------------------------- #

    def read_slow(self) -> dict:
        parts = []
        for p in psutil.disk_partitions(all=False):
            if "cdrom" in p.opts or p.fstype == "":
                continue
            try:
                u = psutil.disk_usage(p.mountpoint)
            except (PermissionError, OSError):
                continue
            if u.total < 2 ** 30:          # skip loop/overlay/pseudo mounts
                continue
            parts.append({
                "device": p.device, "mount": p.mountpoint, "fs": p.fstype,
                "used": u.used / 2**30, "total": u.total / 2**30, "pct": u.percent,
            })

        procs = []
        for pr in psutil.process_iter(["name", "memory_info", "cpu_percent"]):
            try:
                mi = pr.info["memory_info"]
                procs.append({
                    "pid": pr.pid,
                    "name": pr.info["name"] or "?",
                    "rss": (mi.rss / 2**20) if mi else 0.0,
                    "cpu": pr.info["cpu_percent"] or 0.0,
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        procs.sort(key=lambda x: x["rss"], reverse=True)

        self._slow_extras = {"partitions": parts, "procs": procs[:12],
                             "count": len(procs)}
        return self._slow_extras

    @property
    def slow_extras(self) -> dict:
        return self._slow_extras


# --------------------------------------------------------------------------- #
# canonical metric resolution
# --------------------------------------------------------------------------- #

def _best(readings, kind, prefer, group=None):
    """Pick the most representative sensor: first name match wins, else max."""
    pool = [r for r in readings if r.kind == kind and r.value is not None
            and (group is None or r.group == group)]
    if not pool:
        return None
    labels = [r.label.lower() for r in pool]
    for token in prefer:
        for r, low in zip(pool, labels):
            if token in low:
                return r
    return max(pool, key=lambda r: r.value)


def derive(snap: Snapshot) -> None:
    """Fold vendor-specific sensor names into stable canonical keys."""
    vals = list(snap.values())
    cpu = [r for r in vals if r.group == "CPU"]
    gpu = [r for r in vals if r.group == "GPU"]

    t = _best(cpu, "temp", ("cpu package", "core (tctl/tdie)", "core (tctl)",
                            "cpu die", "package id 0", "tctl", "core max", "cpu"))
    if t:
        snap.put("cpu.temp", "CPU temperature", t.value, "°C", "temp", "CPU", t.source)

    c = _best(cpu, "clock", ("core #1", "core 0", "cpu core"))
    if c and c.value:
        snap.put("cpu.clock", "CPU clock", max(
            (r.value for r in cpu if r.kind == "clock" and r.value
             and "bus" not in r.label.lower()), default=c.value),
            "MHz", "clock", "CPU", c.source)

    p = _best(cpu, "power", ("package", "cpu package", "total"))
    if p:
        snap.put("cpu.power", "CPU power", p.value, "W", "power", "CPU", p.source)

    f = _best(cpu, "fan", ("cpu",)) or _best(vals, "fan", ("cpu fan", "fan #1"))
    if f:
        snap.put("cpu.fan", "CPU fan", f.value, "RPM", "fan", "CPU", f.source)

    if gpu:
        for canon, kind, prefer in (
            ("gpu.temp", "temp", ("gpu core", "gpu hot spot", "gpu")),
            ("gpu.load", "load", ("gpu core", "gpu")),
            ("gpu.clock", "clock", ("gpu core", "gpu")),
            ("gpu.power", "power", ("gpu package", "gpu power", "gpu")),
            ("gpu.fan", "fan", ("gpu",)),
        ):
            r = _best(gpu, kind, prefer)
            if r:
                snap.put(canon, r.label, r.value, r.unit, kind, "GPU", r.source)
        used = _best(gpu, "data", ("gpu memory used", "memory used"))
        total = _best(gpu, "data", ("gpu memory total", "memory total"))
        if used:
            snap.put("vram.used", "VRAM used", used.value, used.unit, "data", "GPU", used.source)
        if total and total.value:
            snap.put("vram.total", "VRAM total", total.value, total.unit, "data", "GPU", total.source)
            if used:
                snap.put("vram.pct", "VRAM usage",
                         100.0 * used.value / total.value, "%", "load", "GPU", used.source)

    st = [r for r in vals if r.group == "Storage" and r.kind == "temp" and r.value]
    if st:
        hottest = max(st, key=lambda r: r.value)
        snap.put("disk.temp", "Drive temperature", hottest.value, "°C", "temp",
                 "Storage", hottest.source)


# --------------------------------------------------------------------------- #
# sampler thread
# --------------------------------------------------------------------------- #

TRACKED = ("cpu.load", "cpu.temp", "cpu.clock", "cpu.power",
           "gpu.load", "gpu.temp", "gpu.clock", "gpu.power",
           "ram.pct", "vram.pct", "net.down", "net.up",
           "disk.read", "disk.write")


class Sampler(threading.Thread):
    """Single polling thread. Publishes an immutable Snapshot per cycle."""

    def __init__(self, interval: float = 1.0, slow_every: int = 10):
        super().__init__(daemon=True, name="hwmon-sampler")
        self.interval = interval
        self.slow_every = slow_every
        self.backend_note = "starting…"
        self.lhm_error: Optional[str] = None

        self._halt = threading.Event()
        self._nudge = threading.Event()
        self._paused = False
        self._published: Optional[Snapshot] = None
        self._history: Dict[str, deque] = {k: deque(maxlen=HISTORY_LEN) for k in TRACKED}
        self._psutil = PsutilBackend()
        self._lhm: Optional[LibreHardwareBackend] = None

    # -- public API --------------------------------------------------------- #

    def snapshot(self) -> Optional[Snapshot]:
        return self._published  # atomic ref read; never mutated after publish

    def history(self, key: str) -> List[float]:
        return list(self._history.get(key, ()))

    def set_interval(self, seconds: float) -> None:
        if abs(seconds - self.interval) > 1e-6:
            self.interval = seconds
            self._nudge.set()

    def set_paused(self, paused: bool) -> None:
        self._paused = paused
        self._nudge.set()

    def stop(self) -> None:
        self._halt.set()
        self._nudge.set()

    # -- thread body -------------------------------------------------------- #

    def run(self) -> None:
        self._open_lhm()
        cycle = 0
        while not self._halt.is_set():
            start = time.monotonic()
            if not self._paused:
                try:
                    self._cycle(cycle)
                    cycle += 1
                except Exception as exc:  # never let the thread die
                    self.backend_note = f"sampling error: {exc}"
            wait = max(self.interval - (time.monotonic() - start), 0.02)
            self._nudge.wait(wait)
            self._nudge.clear()
        if self._lhm:
            self._lhm.close()

    def _open_lhm(self) -> None:
        if not IS_WIN:
            self.backend_note = "psutil (Linux sysfs sensors)"
            return
        try:
            self._lhm = LibreHardwareBackend()
            self.backend_note = ("psutil + LibreHardwareMonitor"
                                 if is_admin() else
                                 "psutil + LHM (run as admin for full sensors)")
        except FileNotFoundError as exc:
            self.lhm_error = str(exc)
            self.backend_note = "psutil only — LibreHardwareMonitorLib.dll not found in lib/"
        except Exception as exc:
            self.lhm_error = f"{type(exc).__name__}: {exc}"
            self.backend_note = f"psutil only — LHM failed to load ({type(exc).__name__})"
            print(f"[sensors] LibreHardwareMonitor unavailable: {self.lhm_error}\n"
                  f"[sensors] run diagnose.py for details", file=sys.stderr)

    def _cycle(self, cycle: int) -> None:
        snap = Snapshot()
        self._psutil.read_fast(snap)
        if cycle % self.slow_every == 0:
            self._psutil.read_slow()
        extras = self._psutil.slow_extras
        snap.partitions = extras.get("partitions", [])
        snap.procs = extras.get("procs", [])
        snap.meta["proc.count"] = str(extras.get("count", ""))

        if self._lhm:
            try:
                self._lhm.read(snap)
            except Exception as exc:
                self.lhm_error = str(exc)

        derive(snap)
        snap.meta.setdefault("cpu.name", _fallback_cpu_name())
        for k in TRACKED:
            v = snap.val(k)
            if v is not None:
                self._history[k].append(v)
        self._published = snap


def _fallback_cpu_name() -> str:
    import platform
    return platform.processor() or platform.machine() or "Processor"