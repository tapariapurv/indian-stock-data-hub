"""
How the app runs on this machine: how much memory it is using, and whether
it starts when you log in.

Everything here is standard library and reversible. Starting with the
computer writes exactly one file, in the place each operating system
documents for it, and switching it off deletes that file again -- no
installer, no service, nothing left behind.
"""

import os
import plistlib
import subprocess
import sys
from pathlib import Path

try:
    import resource           # Unix only; Windows is handled below
except ImportError:
    resource = None

APP_DIR = Path(__file__).resolve().parent
APP_FILE = APP_DIR / "app.py"
LABEL = "com.stockdatahub.app"
PORT = 8501


def memory_mb() -> float:
    """Resident memory for this process, in MB, with no third-party package.

    ru_maxrss is bytes on macOS and kilobytes on Linux -- the classic way to
    report a number 1024x wrong. Windows has no `resource` module at all, so
    it asks the OS directly. Returns 0.0 where it cannot be read, and the UI
    shows a dash rather than a wrong number.
    """
    if resource is not None:
        try:
            peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        except (ValueError, OSError):
            return 0.0
        return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024
    try:  # Windows
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]

        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        if ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            return counters.PeakWorkingSetSize / (1024 * 1024)
    except Exception:
        pass
    return 0.0


# --------------------------------------------------------------------------
# Starting with the computer
# --------------------------------------------------------------------------

def command() -> list[str]:
    """The command that starts the app, using the very interpreter running
    now -- so a virtual environment keeps working after a reboot."""
    return [sys.executable, "-m", "streamlit", "run", str(APP_FILE),
            "--server.headless", "true", "--server.port", str(PORT)]


def target() -> Path:
    """The one file that gets written, per platform convention."""
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    if os.name == "nt":
        return (home / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu"
                / "Programs" / "Startup" / "Indian Stock Data Hub.cmd")
    return home / ".config" / "autostart" / "stock-data-hub.desktop"


DESCRIPTION = ({
    "darwin": "On this Mac that means a LaunchAgent in ~/Library/LaunchAgents.",
    "win32": "On Windows that means a shortcut in your Startup folder.",
}.get(sys.platform, "On Linux that means a .desktop file in ~/.config/autostart."))


def _plist() -> bytes:
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": command(),
        "WorkingDirectory": str(APP_DIR),
        "RunAtLoad": True,
        # Restart if it ever exits, but never in a tight loop.
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 60,
        "StandardOutPath": str(APP_DIR / "autostart.log"),
        "StandardErrorPath": str(APP_DIR / "autostart.log"),
        "ProcessType": "Background",
        "LowPriorityIO": True,
        # Background, not a foreground app: it must not steal focus at login.
        "Nice": 5,
    })


def _desktop() -> str:
    return ("[Desktop Entry]\n"
            "Type=Application\n"
            "Name=Indian Stock Data Hub\n"
            "Comment=Research desk for Indian listed companies\n"
            f"Exec={' '.join(command())}\n"
            f"Path={APP_DIR}\n"
            "Terminal=false\n"
            "X-GNOME-Autostart-enabled=true\n")


def _cmd_script() -> str:
    quoted = " ".join(f'"{part}"' if " " in part else part for part in command())
    return ("@echo off\r\n"
            "rem Starts Indian Stock Data Hub in the background at login.\r\n"
            f'cd /d "{APP_DIR}"\r\n'
            f"start \"\" /min {quoted}\r\n")


def contents() -> bytes:
    """What would be written, without writing it (this is what the tests read)."""
    if sys.platform == "darwin":
        return _plist()
    if os.name == "nt":
        return _cmd_script().encode()
    return _desktop().encode()


def status() -> tuple[bool, Path]:
    path = target()
    return path.exists(), path


def enable() -> Path:
    path = target()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contents())
    if os.name != "nt":
        path.chmod(0o644 if sys.platform == "darwin" else 0o755)
    if sys.platform == "darwin":
        # Registers it for this login session too, so it works without a reboot.
        # bootstrap is the modern verb; load is kept for older macOS.
        uid = os.getuid()
        for argv in (["launchctl", "bootstrap", f"gui/{uid}", str(path)],
                     ["launchctl", "load", "-w", str(path)]):
            try:
                if subprocess.run(argv, capture_output=True, timeout=10).returncode == 0:
                    break
            except (OSError, subprocess.SubprocessError):
                pass  # the file is written; it will still run at next login
    return path


def disable() -> None:
    path = target()
    if sys.platform == "darwin" and path.exists():
        uid = os.getuid()
        for argv in (["launchctl", "bootout", f"gui/{uid}/{LABEL}"],
                     ["launchctl", "unload", "-w", str(path)]):
            try:
                if subprocess.run(argv, capture_output=True, timeout=10).returncode == 0:
                    break
            except (OSError, subprocess.SubprocessError):
                pass
    path.unlink(missing_ok=True)


if __name__ == "__main__":
    print(f"memory now : {memory_mb():.1f} MB")
    print(f"would write: {target()}")
    print(f"command    : {' '.join(command())}")
    installed, where = status()
    print(f"installed  : {installed} ({where})")
