# /opt/fptv/python/fptv/mpv.py
from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from typing import Optional, List

from fptv.log import Logger

MPV_SOCK = "/tmp/fptv-mpv.sock"


class MPV:
    """
    mpv controller intended for appliance/KMS usage.

    Key behaviors vs the X11 version:
    - No DISPLAY or wm/window assumptions.
    - mpv runs fullscreen directly on DRM/KMS (vo=gpu, gpu-context=drm).
    - IPC is used for channel changes without restarting mpv.
    - Provides explicit `stop()` (stay alive + black) and `shutdown()` (release DRM).
    """

    def __init__(self, sock_path: str = MPV_SOCK) -> None:
        self.proc: Optional[subprocess.Popen] = None
        self.sock_path = sock_path
        self.log = Logger("mpv")

    # ---- public API ----

    def spawn(self) -> None:
        """Start mpv if not already running; wait for IPC socket readiness."""
        if self._is_running():
            return

        # Remove any stale socket.
        try:
            os.unlink(self.sock_path)
        except FileNotFoundError:
            pass
        except Exception as e:
            self.log.err(f"mpv: Could not unlink stale socket {self.sock_path}: {e}")

        cmd = self._build_cmd()
        self.log.out(f"Exec: {cmd}")

        # Important: do not set DISPLAY; this is for console/KMS.
        # In systemd console mode, mpv will grab DRM/KMS.
        try:
            self.proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except Exception as e:
            self.log.err(f"mpv: Failed to start: {e}")
            self.proc = None
            return

        self.log.out(f"mpv pid: {self.proc.pid}")

        # If it died immediately, capture output to help debugging.
        rc = self.proc.poll()
        if rc is not None:
            try:
                out, err = self.proc.communicate(timeout=0.5)
            except Exception:
                out, err = ("", "")
            self.log.err(f"mpv exited immediately rc={rc}")
            if out:
                self.log.out(f"mpv stdout: {out.strip()}")
            if err:
                self.log.err(f"mpv stderr: {err.strip()}")
            self.proc = None
            return

        if not self._wait_for_socket(timeout_s=5.0):
            self.log.err("mpv: IPC socket never appeared; shutting down mpv.")
            self.shutdown()

    def play(self, url: str) -> None:
        """
        Load and play a stream URL. mpv remains running between plays to reduce flicker.
        """
        self.spawn()
        if not self._is_running():
            self.log.err("mpv: Not running; cannot play.")
            return

        self.log.out(f"Playing: {url}")

        # Load the stream, replacing anything currently playing.
        ok = self._cmd(["loadfile", url, "replace"])
        if not ok:
            self.log.err("mpv: loadfile failed; restarting mpv and retrying once.")
            self.shutdown()
            self.spawn()
            if self._is_running():
                self._cmd(["loadfile", url, "replace"])

        # Ensure playback is not paused.
        self._cmd(["set_property", "pause", False])

    def stop(self) -> None:
        """
        Stop playback but keep mpv alive (idle) to avoid restart flicker.
        Leaves the screen black (typically) while idle.
        """
        if not self._is_running():
            self.proc = None
            return

        # Stop playback and clear playlist; keep process alive in idle.
        self._cmd(["stop"])
        self._cmd(["playlist-clear"])
        self._cmd(["set_property", "pause", True])

        # Some builds keep last frame; try to force video off.
        # This is safe even if it doesn't do anything in DRM mode.
        self._cmd(["set_property", "vid", "no"])

    def shutdown(self) -> None:
        """
        Terminate mpv so it releases DRM/KMS. Use this before re-initializing pygame UI.
        """
        self.log.out("mpv: Begin shutdown")

        if not self.proc:
            self.log.out("mpv: No process. Nothing to shut down.")
            return

        if self.proc.poll() is not None:
            self.log.out("mpv: Process already exited.")
            self.proc = None
            return

        # Try to quit via IPC first (cleanest).
        self.log.out("mpv: Trying to shut down nicely (IPC quit).")
        self._cmd(["quit"])

        try:
            self.proc.wait(timeout=2.0)
        except Exception:
            self.log.out("mpv: Timed out waiting for quit; terminating.")
            try:
                self.proc.terminate()
            except Exception:
                pass
            try:
                self.proc.wait(timeout=2.0)
            except Exception:
                self.log.out("mpv: Still running; killing.")
                try:
                    self.proc.kill()
                except Exception:
                    pass
        finally:
            self.proc = None

        # Remove the socket if it remains.
        try:
            os.unlink(self.sock_path)
        except FileNotFoundError:
            pass
        except Exception:
            pass

    # ---- internals ----

    def _build_cmd(self) -> List[str]:
        """
        Build an mpv command line suitable for console/KMS mode.
        """
        return [
            "mpv",
            f"--input-ipc-server={self.sock_path}",
            "--idle=yes",
            "--keep-open=yes",
            "--fullscreen",
            "--no-terminal",
            "--really-quiet",
            "--osc=no",
            "--osd-level=0",
            "--no-input-default-bindings",
            "--image-display-duration=0",

            # DRM/KMS output
            "--vo=gpu",
            "--gpu-context=drm",
            "--hwdec=auto",
        ]

    def _cmd(self, cmd: list) -> bool:
        """
        Execute a command like ["stop"] or ["loadfile", url, "replace"] over mpv IPC.
        """
        if not os.path.exists(self.sock_path):
            # Socket may not exist yet if mpv is starting; caller should spawn() + wait.
            self.log.err(f"mpv: IPC socket not found: {self.sock_path}")
            return False

        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(0.2)
                s.connect(self.sock_path)
                dumped = json.dumps({"command": cmd}) + "\n"
                s.sendall(dumped.encode("utf-8"))
            return True
        except OSError as e:
            self.log.err(f"mpv: OSError in command {cmd}: {e}")
            return False

    def _wait_for_socket(self, timeout_s: float = 5.0) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if os.path.exists(self.sock_path):
                return True

            # If mpv died while we waited, surface that.
            if self.proc and self.proc.poll() is not None:
                try:
                    out, err = self.proc.communicate(timeout=0.2)
                except Exception:
                    out, err = ("", "")
                    self.log.err(f"mpv exited while waiting for socket rc={self.proc.poll()}")
                if out:
                    self.log.out(f"mpv stdout: {out.strip()}")
                if err:
                    self.log.err(f"mpv stderr: {err.strip()}")
                self.proc = None
                return False

            time.sleep(0.02)

        self.log.err("mpv: Timed out waiting for IPC socket")
        return False

    def _is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None
