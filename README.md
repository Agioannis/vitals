# Vitals — a lightweight hardware monitor in Python

Live CPU / GPU / memory / storage / network telemetry with real temperatures,
per-core clocks and fan RPM. Dark instrument-panel UI built on CustomTkinter.

```
main.py       window shell, sidebar, refresh loop
ui.py         pages, cards, canvas sparklines, formatting
sensors.py    backends (psutil + LibreHardwareMonitor) and the sampler thread
```

## Install

```bash
pip install -r requirements.txt
python main.py
```

Optional flag: `python main.py --interval 2` starts at a 2-second cadence.

## Getting real temperatures on Windows

`psutil` alone cannot read temperatures, fan speeds or per-core clocks on
Windows — nothing in pure Python can. Vitals gets them from
**LibreHardwareMonitorLib.dll**, the same engine HWiNFO-class tools use.

1. Download the latest LibreHardwareMonitor release zip from its GitHub
   releases page.
2. Copy `LibreHardwareMonitorLib.dll` **and** `HidSharp.dll` into a `lib/`
   folder next to `sensors.py`. (Or point the `LHM_DLL` environment variable at
   the DLL.)
3. Run your terminal **as administrator** — the library loads a kernel driver to
   read MSRs and SMBus. Without admin you still get load, clocks-via-psutil,
   memory, disks and network, but the temperature tiles stay blank and the
   header shows a warning.

Without the DLL the app runs fine, just with fewer sensors. On Linux it reads
`/sys` sensors through psutil and needs no extra setup.

## How it stays cheap

The "monitor's own footprint" panel on the System page shows the app's live CPU
and RSS, so you can verify this rather than take my word for it.

| Technique | Effect |
|---|---|
| One background thread | The Tk main loop never blocks on a sensor read |
| Two-tier polling | Partition scans and the process list run every 10th cycle, not every cycle |
| Only the visible page refreshes | Six of the seven pages cost nothing while hidden |
| No widget churn | Widgets are built once; ticks only push text into `StringVar`s |
| Canvas sparklines via `coords()` | Existing line items are moved, not recreated. No matplotlib, no image blitting |
| Idle backoff | Minimising the window drops the sampler to one sample per 5 s |
| Adjustable cadence | 0.5 / 1 / 2 / 5 s from the sidebar, plus a hard pause switch |

Cost scales with the interval you pick, the core count, and whether the LHM
backend is loaded (it is the expensive part — it talks to the SMBus and the
GPU driver). If you want it as close to free as possible, run at 2 s.

## Adding a metric

1. Emit a `Reading` from a backend in `sensors.py`, or map a vendor sensor name
   to a canonical key in `derive()`.
2. Add the key to `TRACKED` if you want a history graph for it.
3. Read it in a page's `refresh()` with `s.val("your.key")`.

## Known limits

- `psutil.cpu_freq()` on Windows reports the base clock, not the live one. Real
  boost clocks come from the LHM backend, so install the DLL if clock accuracy
  matters to you.
- Intel/AMD integrated GPUs expose far fewer sensors than discrete cards.
- Some laptop EC sensors are only visible to the vendor's own utility.

## Packaging

```bash
pip install pyinstaller
pyinstaller --noconsole --onefile --add-data "lib;lib" main.py
```

The exe needs to run elevated for the same reason the script does. Add a
`uac-admin` manifest, or just right-click → Run as administrator.
