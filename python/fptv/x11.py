from __future__ import annotations

import subprocess
from typing import List


def blanking_disable() -> None:
    """
    Disable X screen saver + DPMS so the display stays on.
    Requires an X session (your Pi Desktop).
    """
    run_quiet(["xset", "s", "off"])
    run_quiet(["xset", "-dpms"])
    run_quiet(["xset", "s", "noblank"])


def blanking_enable() -> None:
    """
    Re-enable defaults (you can tune these).
    """
    run_quiet(["xset", "s", "on"])
    run_quiet(["xset", "+dpms"])
    run_quiet(["xset", "s", "blank"])


def mpv_raise() -> None:
    subprocess.run(["wmctrl", "-r", "mpv-fptv", "-b", "add,above"], check=False)
    subprocess.run(["wmctrl", "-a", "mpv-fptv"], check=False)


def mpv_lower() -> None:
    subprocess.run(["wmctrl", "-r", "mpv-fptv", "-b", "remove,above"], check=False)
    subprocess.run(["wmctrl", "-r", "mpv-fptv", "-b", "add,below"], check=False)


def ui_show(ui_xid: str) -> None:
    subprocess.run(["wmctrl", "-i", "-r", ui_xid, "-b", "remove,hidden,below"], check=False)
    subprocess.run(["wmctrl", "-i", "-r", ui_xid, "-b", "add,above,sticky,skip_taskbar,skip_pager"], check=False)
    subprocess.run(["wmctrl", "-i", "-a", ui_xid], check=False)


def ui_hide(ui_xid: str) -> None:
    subprocess.run(["wmctrl", "-i", "-r", ui_xid, "-b", "add,hidden"], check=False)


def run_quiet(cmd: List[str]) -> None:
    try:
        subprocess.run(cmd, check=False,
                       stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
    except Exception:
        pass


UI_WIN = "fptv.fptv"
