# Vitals — a lightweight hardware monitor in Python

Live CPU / GPU / memory / storage / network telemetry with real temperatures,
per-core clocks and fan RPM. Dark instrument-panel UI built on CustomTkinter.

```
main.py       window shell, sidebar, refresh loop
ui.py         pages, cards, canvas sparklines, formatting
sensors.py    backends (psutil + LibreHardwareMonitor) and the sampler thread
```

## Get it

**Prebuilt (Windows, no Python needed):** grab the latest zip from
[Releases](../../releases), unzip, and run `Vitals.exe` — it needs the
`_internal` folder next to it. Right-click → Run as administrator for full
sensor access.

**From source:**

```bash
pip install -r requirements.txt
python main.py
```

Optional flags: `python main.py --interval 2` starts at a 2-second cadence;
`python main.py --version` prints the version.

## Getting real temperatures on Windows

`psutil` alone cannot read temperatures, fan speeds or per-core clocks on
Windows — nothing in pure Python can. Vitals gets them from
**LibreHardwareMonitorLib.dll**, the same engine HWiNFO-class tools use.

The DLL isn't checked into this repo (it's a third-party binary — see
`.gitignore`), so **the first time you run `python main.py` with no `lib/`
folder, Vitals downloads it automatically** from LibreHardwareMonitor's GitHub
release, in the background, on the sampler thread — no setup step, no
blocking the UI. The sidebar shows "fetching sensor library…" while that
happens. If you'd rather do it yourself (offline install, corporate proxy,
etc.), `get-lhm.ps1` does the same thing from NuGet, or you can copy
`LibreHardwareMonitorLib.dll` and `HidSharp.dll` into `lib/` by hand (or point
the `LHM_DLL` environment variable at the DLL).

Either way, run your terminal **as administrator** — the library loads a
kernel driver to read MSRs and SMBus. Without admin you still get load,
clocks-via-psutil, memory, disks and network, but the temperature tiles stay
blank and the header shows a warning. Note: if Windows **Memory Integrity /
Core Isolation** is turned on, it blocks that kernel driver outright even when
elevated — CPU temperature/clock/power will stay blank (GPU and disk temps
are unaffected, they don't need it) until you disable Memory Integrity in
Windows Security and reboot.

Without the DLL the app runs fine, just with fewer sensors. On Linux it reads
`/sys` sensors through psutil and needs no extra setup.

## CPU affinity

On launch, Vitals checks per-core load for ~150 ms and pins itself to
whichever logical core is least busy at that moment (`psutil.Process().
cpu_affinity()`), so it competes as little as possible with whatever else is
running. This is a one-time decision made at startup, not continuously
re-balanced. Check the System page for which core it picked. Not supported on
macOS; failures there are silent and the process just runs unpinned.

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

Pushing a tag like `v1.0.1` triggers `.github/workflows/release.yml`, which
builds this and attaches the zip to a new GitHub Release automatically. To
build locally instead:

```bash
pip install pyinstaller
pyinstaller --noconsole --onedir --noupx --uac-admin --name Vitals --add-data "lib;lib" main.py
```

`--uac-admin` embeds a manifest so Windows prompts for elevation on launch,
for the same reason the script needs an admin terminal. `--onedir` (a folder)
rather than `--onefile` (a self-extracting single exe) is deliberate:
`--onefile`'s runtime self-extraction is one of the most common patterns
antivirus heuristics flag as dropper-like behavior in unsigned indie builds,
and `--onedir` avoids it. Zip the whole `dist/Vitals/` folder to distribute it
— the exe needs its `_internal` folder alongside it.

If Defender or a friend's antivirus still flags the build, that's a
false-positive review at https://www.microsoft.com/en-us/wdsi/filesubmission,
not a bug here. For a durable fix, [SignPath.io](https://signpath.io) issues
free code-signing certificates to open-source projects — a signed build is
trusted far more readily by both Defender and SmartScreen.

## License

[MIT](LICENSE)
