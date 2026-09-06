"""
diagnose.py — works out why temperatures / GPU / motherboard are missing.

    python diagnose.py

Run it twice: once normally, once from an administrator terminal. Compare.
"""

from __future__ import annotations

import ctypes
import os
import platform
import sys
import traceback

OK, BAD, WARN, INFO = "[ OK ]", "[FAIL]", "[WARN]", "[ .. ]"
HERE = os.path.dirname(os.path.abspath(__file__))


def hr(title: str) -> None:
    print(f"\n{'-' * 68}\n{title}\n{'-' * 68}")


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# --------------------------------------------------------------------------- #

def step_environment() -> None:
    hr("1. Environment")
    print(f"{INFO} Python      {sys.version.split()[0]} ({platform.architecture()[0]})")
    print(f"{INFO} Executable  {sys.executable}")
    print(f"{INFO} OS          {platform.system()} {platform.release()}")
    if not sys.platform.startswith("win"):
        print(f"{WARN} Not Windows — the LHM backend is Windows-only.")
        return
    if is_admin():
        print(f"{OK} Running as administrator.")
    else:
        print(f"{BAD} NOT running as administrator.")
        print("       LHM loads a kernel driver to read temperature registers.")
        print("       Without elevation it opens but reports almost no sensors.")


def step_dll() -> str | None:
    hr("2. LibreHardwareMonitor DLLs")
    libdir = os.path.join(HERE, "lib")
    if not os.path.isdir(libdir):
        print(f"{BAD} No lib\\ folder at {libdir}")
        return None

    contents = os.listdir(libdir)
    print(f"{INFO} lib\\ contains: {contents or '(empty)'}")

    main_dll = os.path.join(libdir, "LibreHardwareMonitorLib.dll")
    if not os.path.isfile(main_dll):
        print(f"{BAD} LibreHardwareMonitorLib.dll is NOT in lib\\.")
        print("       Download the LibreHardwareMonitor release zip from GitHub,")
        print("       open it, and copy the DLL out of it. Copying the .zip itself")
        print("       or a shortcut will not work.")
        return None
    size = os.path.getsize(main_dll)
    print(f"{OK} LibreHardwareMonitorLib.dll found ({size:,} bytes)")
    if size < 100_000:
        print(f"{WARN} That looks too small — possibly an LFS pointer or a bad copy.")

    hid = os.path.join(libdir, "HidSharp.dll")
    if os.path.isfile(hid):
        print(f"{OK} HidSharp.dll found")
    else:
        print(f"{BAD} HidSharp.dll missing — LHM needs it and will fail to load.")

    # The classic one: Windows marks files extracted from a downloaded zip and
    # .NET then refuses to load the assembly.
    blocked = [f for f in ("LibreHardwareMonitorLib.dll", "HidSharp.dll")
               if os.path.exists(os.path.join(libdir, f) + ":Zone.Identifier")]
    if blocked:
        print(f"{BAD} These files are BLOCKED by Windows (mark-of-the-web): {blocked}")
        print("       Fix, in PowerShell, from the project folder:")
        print("           Get-ChildItem .\\lib\\*.dll | Unblock-File")
    else:
        print(f"{OK} No mark-of-the-web block on the DLLs.")
    return main_dll


def step_runtime() -> None:
    hr("3. .NET runtime")
    try:
        import clr_loader
        print(f"{OK} clr_loader {getattr(clr_loader, '__version__', '?')} importable")
    except Exception as exc:
        print(f"{BAD} clr_loader import failed: {exc}")
        return

    forced = os.environ.get("PYTHONNET_RUNTIME")
    print(f"{INFO} PYTHONNET_RUNTIME = {forced or '(unset, defaults to .NET Framework)'}")

    try:
        clr_loader.get_netfx()
        print(f"{OK} .NET Framework 4.x runtime available")
    except Exception as exc:
        print(f"{WARN} .NET Framework not usable: {exc}")
    try:
        clr_loader.find_dotnet_root()
        print(f"{OK} .NET (Core) runtime installed")
    except Exception:
        print(f"{INFO} No .NET Core/8 runtime found (only needed for net8.0 LHM builds)")


def step_load(dll: str | None) -> None:
    hr("4. Loading the assembly")
    if dll is None:
        print(f"{INFO} Skipped — no DLL to load.")
        return
    try:
        import clr
    except Exception:
        print(f"{BAD} 'import clr' failed. pythonnet is not installed correctly.")
        traceback.print_exc()
        return
    print(f"{OK} pythonnet imported")

    try:
        sys.path.append(os.path.dirname(dll))
        clr.AddReference(os.path.splitext(dll)[0])
        print(f"{OK} AddReference succeeded")
    except Exception:
        print(f"{BAD} AddReference failed. Full error:")
        traceback.print_exc()
        print("\n       Most common causes, in order:")
        print("       1. DLL blocked by Windows      -> Unblock-File (see step 2)")
        print("       2. DLL targets net8.0          -> install .NET 8 Desktop Runtime")
        print("                                         and set PYTHONNET_RUNTIME=coreclr")
        print("       3. HidSharp.dll missing        -> copy it next to the main DLL")
        print("       4. 32-bit Python + 64-bit DLL  -> use 64-bit Python")
        return

    try:
        from LibreHardwareMonitor import Hardware
        print(f"{OK} LibreHardwareMonitor namespace imported")
    except Exception:
        print(f"{BAD} Namespace import failed:")
        traceback.print_exc()
        return

    try:
        c = Hardware.Computer()
        c.IsCpuEnabled = True
        c.IsGpuEnabled = True
        c.IsMemoryEnabled = True
        c.IsMotherboardEnabled = True
        c.IsStorageEnabled = True
        c.IsControllerEnabled = True
        c.Open()
        print(f"{OK} Computer.Open() succeeded")
    except Exception:
        print(f"{BAD} Computer.Open() failed — this is almost always the driver:")
        traceback.print_exc()
        return

    hr("5. What the hardware actually reports")
    found = 0
    for hw in c.Hardware:
        hw.Update()
        sensors = list(hw.Sensors)
        for sub in hw.SubHardware:
            sub.Update()
            sensors += list(sub.Sensors)
        found += 1
        temps = [s for s in sensors if str(s.SensorType) == "Temperature"]
        print(f"\n  {str(hw.HardwareType):<14} {hw.Name}")
        print(f"    {len(sensors)} sensors, {len(temps)} temperature")
        for s in sensors[:60]:
            v = s.Value
            print(f"      {str(s.SensorType):<12} {str(s.Name):<28} "
                  f"{'None' if v is None else f'{float(v):.2f}'}")
    if found == 0:
        print(f"{BAD} Zero hardware objects. LHM loaded but the driver did not start.")
        print("       Run this from an administrator terminal.")
    try:
        c.Close()
    except Exception:
        pass


def main() -> None:
    print("=" * 68)
    print("  Vitals — sensor backend diagnostics")
    print("=" * 68)
    step_environment()
    dll = step_dll()
    step_runtime()
    step_load(dll)
    print("\nDone. Paste this whole output back if anything says [FAIL].\n")


if __name__ == "__main__":
    main()
