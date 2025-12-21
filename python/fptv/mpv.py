from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from log import Logger
from typing import Optional

MPV_SOCK = "/tmp/fptv-mpv.sock"


class MPV:
    def __init__(self, sock_path: str = MPV_SOCK) -> None:
        self.proc: Optional[subprocess.Popen] = None
        self.sock_path = sock_path
        self.log = Logger("mpv")

    def spawn(self) -> None:
        if self._is_running():
            return

        env = os.environ.copy()
        env.setdefault("DISPLAY", ":0")

        # Remove any stale socket.
        try:
            os.unlink(self.sock_path)
        except FileNotFoundError:
            pass

        cmd = [
            "mpv",
            f"--input-ipc-server={self.sock_path}",
            "--idle=yes",
            "--force-window=yes",

            "--keep-open=yes",  # Keep window alive
            "--fullscreen",
            "--ontop=no",
            "--title=mpv-fptv",
            "--no-border",

            "--osc=no",
            "--osd-level=0",
            "--no-terminal",
            "--really-quiet",

            "--image-display-duration=0",
            "--no-input-default-bindings",
            "--background=color",  # Make it invisible on startup
            "--background-color=#000000",
            # Optional - may reduce latency / buffering lag
            # "--cache=no",
            # "--untimed=yes",
        ]
        self.log.out(f"Exec: {cmd}")

        self.proc = subprocess.Popen(
            cmd,
            env=env,
            # start_new_session=True, # New process group
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)

        self.log.out(f"mpv pid: {self.proc.pid}")
        rc = self.proc.poll()
        if rc is not None:
            out, err = self.proc.communicate(timeout=0.2)
            self.log.out(f"mpv stdout: {out}")
            self.log.err(f"mpv stderr: {err}")
            self.proc = None
            return

        self._wait_for_socket()

    def play(self, url: str) -> None:
        self.spawn()
        self.log.out(f"Playing: {url}")

        # Make player visible again.
        self._cmd(["set_property", "vid", "auto"])

        ok = self._cmd(["loadfile", url, "replace"])
        if not ok:
            self.log.err("Error playing. Trying to restart.")
            self.shutdown()
            self.spawn()
            self._cmd(["loadfile", url, "replace"])

        self._cmd(["set_property", "pause", False])

    def stop(self) -> None:
        if not self.proc:
            return

        if self.proc.poll() is not None:
            self.proc = None
            return

        # Stop playback, but keep mpv running.
        self._cmd(["stop"])
        self._cmd(["set_property", "pause", True])
        # Force black screen
        self._cmd(["set_property", "vid", "no"])

    def shutdown(self) -> None:
        self.log.out("mpv: Begin shutdown")
        if not self.proc:
            self.log.out("mpv: No process. Nothing to shut down.")
            return

        if self.proc.poll() is not None:
            self.log.out("mpv: Nothing to shut down.")
            self.proc = None
            return

        # Try to shutdown nicely.
        self.log.out("mpv: Trying to shut down nicely.")
        if not self._cmd(["quit"]):
            self.log.out("mpv: Trying harder to shut down.")
            self.proc.terminate()

        try:
            self.proc.wait(timeout=2)
        except Exception as e:
            self.log.out("mpv: Exception waiting for process: {e}. Now killing process.")
            self.proc.kill()
        finally:
            self.proc = None

    def _cmd(self, cmd: list) -> bool:
        """
        Execute a command like ["stop"] or ["loadfile", url, "replace"]
        """

        if not os.path.exists(self.sock_path):
            self.log.err(f"Not found: {self.sock_path}")
            return False

        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(0.2)
                s.connect(self.sock_path)
                dumped = json.dumps({"command": cmd}) + "\n"
                s.sendall(dumped.encode("utf-8"))
            return True

        except OSError as e:
            self.log.err(f"OSError in command {cmd}: {e}")
            return False

    def _wait_for_socket(self, timeout_s: float = 5.0) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if os.path.exists(self.sock_path):
                return True

            time.sleep(0.02)

        self.log.err("Timed out waiting for socket")
        return False

    def _is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None
