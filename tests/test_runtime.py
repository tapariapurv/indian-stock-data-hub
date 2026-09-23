"""Self-check for memory reporting and starting with the computer.

Nothing here installs anything: the autostart file is generated into a
temporary directory and parsed back, so the real LaunchAgents / Startup /
autostart folder is never touched.  Run: python tests/test_runtime.py
"""
import os
import plistlib
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import runtime  # noqa: E402

# --- Memory ------------------------------------------------------------------
mb = runtime.memory_mb()
assert mb > 0, "memory should be readable"
# The 1024x trap: ru_maxrss is bytes on macOS, kilobytes on Linux. A bare
# interpreter is tens of MB, so anything outside this range means the units
# are wrong rather than the process being enormous.
assert 3 < mb < 4000, f"{mb} MB is not a plausible figure for this process"
print(f"ok: memory reported as {mb:.1f} MB, in sensible units")

# Windows has no `resource` module, so importing this file must not depend on
# one. The fallback has to return a float rather than raise.
_saved = runtime.resource
try:
    runtime.resource = None
    fallback = runtime.memory_mb()
    assert isinstance(fallback, float), fallback
    assert fallback >= 0.0, fallback
finally:
    runtime.resource = _saved
print("ok: memory reporting degrades to a number, not an error, without `resource`")

# --- The command it would run ------------------------------------------------
cmd = runtime.command()
assert cmd[0] == sys.executable, "must relaunch with this interpreter, not a system one"
assert "streamlit" in cmd and "run" in cmd
assert str(runtime.APP_FILE) in cmd, cmd
assert runtime.APP_FILE.exists(), "app.py must be where autostart points"
assert "--server.headless" in cmd, "a login launch must not try to open a terminal"
print("ok: the autostart command points at this app with this interpreter")

# --- The file it would write, on every platform ------------------------------
body = runtime.contents()
assert body, "something has to be written"

if sys.platform == "darwin":
    parsed = plistlib.loads(body)          # must be a valid plist, or launchd ignores it
    assert parsed["Label"] == runtime.LABEL
    assert parsed["ProgramArguments"] == cmd
    assert parsed["RunAtLoad"] is True
    assert parsed["WorkingDirectory"] == str(runtime.APP_DIR)
    # Restart on crash, but never in a tight loop.
    assert parsed["KeepAlive"] == {"SuccessfulExit": False}
    assert parsed["ThrottleInterval"] >= 10, parsed["ThrottleInterval"]
    assert parsed["ProcessType"] == "Background", "it must not take focus at login"
    assert str(runtime.target()).endswith("Library/LaunchAgents/com.stockdatahub.app.plist")
    print("ok: a valid LaunchAgent plist, background priority, restarts without looping")
elif os.name == "nt":
    text = body.decode()
    assert text.startswith("@echo off"), text[:40]
    assert "start \"\" /min" in text, "it should start minimised, not pop a window"
    assert str(runtime.APP_DIR) in text
    assert str(runtime.target()).endswith(".cmd")
    print("ok: a Startup script that launches minimised")
else:
    text = body.decode()
    assert text.startswith("[Desktop Entry]")
    assert "Type=Application" in text and "Terminal=false" in text
    assert f"Exec={' '.join(cmd)}" in text, text
    assert str(runtime.target()).endswith(".desktop")
    print("ok: a valid XDG autostart entry")

# --- Writing and removing it, against a fake home ----------------------------
# status/enable/disable are exercised for real, just not in the real home
# directory -- so a test run can never leave something behind that starts on
# the next login.
tmp = tempfile.TemporaryDirectory()
fake = Path(tmp.name)
real_target = runtime.target
runtime.target = lambda: fake / real_target().name
real_platform = sys.platform
try:
    # Force the plain file path (no launchctl) so nothing touches launchd.
    sys.platform = "linux"
    installed, where = runtime.status()
    assert not installed, "nothing should be installed in a fresh directory"

    path = runtime.enable()
    assert path.exists(), path
    installed, where = runtime.status()
    assert installed and where == path
    assert path.read_bytes(), "the file must not be empty"

    runtime.enable()          # twice in a row must not fail or duplicate
    assert runtime.status()[0]

    runtime.disable()
    assert not runtime.status()[0], "disabling must remove the file"
    runtime.disable()         # removing something already gone must not raise
    print("ok: enable writes one file, disable removes it, and both are repeatable")
finally:
    sys.platform = real_platform
    runtime.target = real_target
    tmp.cleanup()

# The real location is untouched by this test.
assert runtime.status()[1] == real_target(), "the real target must be restored"
print("\nok: runtime reporting and autostart")
