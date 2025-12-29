import ctypes
import json
import os
import socket
import subprocess
import threading
import time
from ctypes import (
    c_void_p, c_char_p, c_int, c_double, c_uint64,
    POINTER, Structure, CFUNCTYPE, byref
)
from ctypes.util import find_library
from typing import Optional, List

from fptv.gl import mpv_opengl_get_proc_address_fn
from fptv.log import Logger

MPV_SOCK = "/tmp/fptv-mpv.sock"

MPV_FORMAT_FLAG = 3

# mpv_render_param_type values (from render.h)
MPV_RENDER_PARAM_INVALID = 0
MPV_RENDER_PARAM_API_TYPE = 1
MPV_RENDER_PARAM_OPENGL_INIT_PARAMS = 2
MPV_RENDER_PARAM_OPENGL_FBO = 3
MPV_RENDER_PARAM_FLIP_Y = 4
MPV_RENDER_PARAM_ADVANCED_CONTROL = 10

# Predefined API type string (from render.h)
MPV_RENDER_API_TYPE_OPENGL = b"opengl"

# mpv_render_update_flag values
MPV_RENDER_UPDATE_FRAME = 1 << 00


class mpv_render_param(Structure):
    _fields_ = [
        ("type", c_int),
        ("data", c_void_p),
    ]


# Optional: minimal mpv_event so we can drain events (not strictly required for playback).
class mpv_event(Structure):
    _fields_ = [
        ("event_id", c_int),
        ("error", c_int),
        ("reply_userdata", c_uint64),
        ("data", c_void_p),
    ]


class mpv_opengl_init_params(Structure):
    _fields_ = [
        ("get_proc_address", mpv_opengl_get_proc_address_fn),
        ("get_proc_address_ctx", c_void_p),
        ("extra_exts", c_char_p),
    ]


class mpv_opengl_fbo(Structure):
    _fields_ = [
        ("fbo", c_int),
        ("w", c_int),
        ("h", c_int),
        ("internal_format", c_int),
    ]


# render.h: typedef void (*mpv_render_update_fn)(void *cb_ctx);
mpv_render_update_fn = CFUNCTYPE(None, c_void_p)


def _load_cdll(names: list[str]) -> ctypes.CDLL:
    last_err = None
    for n in names:
        path = find_library(n) or n
        try:
            return ctypes.CDLL(path)
        except OSError as e:
            last_err = e
    raise OSError(f"Could not load any of: {names}. Last error: {last_err}")


def _try_load_cdll(names: list[str]) -> ctypes.CDLL | None:
    for n in names:
        path = find_library(n) or n
        try:
            return ctypes.CDLL(path)
        except OSError:
            pass
    return None


class EmbeddedMPV:
    """
    A tiny libmpv + render API wrapper.

    The intended use is:
      - init pygame OpenGL display
      - mpv = EmbeddedMPV()
      - mpv.initialize()
      - mpv.loadfile(url)
      - main loop: mpv.maybe_render(width, height); pygame.display.flip()
    """

    def __init__(self) -> None:
        self._mpv = _load_cdll(["mpv", "libmpv.so.2", "libmpv.so.1", "libmpv.so"])
        self._egl = _try_load_cdll(["EGL", "libEGL.so.1", "libEGL.so"])
        self._sdl = _try_load_cdll(["SDL2", "libSDL2-2.0.so.0", "libSDL2.so"])

        # OpenGL/GLES (for glViewport; mpv does not set viewport for you)
        self._gl = _try_load_cdll(["GLESv2", "libGLESv2.so.2", "libGLESv2.so"]) or \
                   _try_load_cdll(["GL", "libGL.so.1", "libGL.so"])
        if self._gl:
            try:
                self._gl.glViewport.argtypes = [c_int, c_int, c_int, c_int]
                self._gl.glViewport.restype = None
            except Exception as e:
                raise RuntimeError(f"Failed to initialize GL viewport: {e}")

        self._handle = c_void_p(None)
        self._render_ctx = c_void_p(None)

        # Thread-safe “poke” from mpv update callback to your loop.
        self._update_event = threading.Event()

        # Keep ctypes callbacks alive
        self._cb_get_proc = mpv_opengl_get_proc_address_fn(self._get_proc_address)
        self._cb_update = mpv_render_update_fn(self._on_mpv_update)

        self._bind_functions()
        print("MVP init complete")

    def _bind_functions(self) -> None:
        # --- core ---
        self._mpv.mpv_create.restype = c_void_p

        self._mpv.mpv_initialize.argtypes = [c_void_p]
        self._mpv.mpv_initialize.restype = c_int

        self._mpv.mpv_terminate_destroy.argtypes = [c_void_p]
        self._mpv.mpv_terminate_destroy.restype = None

        self._mpv.mpv_set_option_string.argtypes = [c_void_p, c_char_p, c_char_p]
        self._mpv.mpv_set_option_string.restype = c_int

        self._mpv.mpv_set_property.argtypes = [c_void_p, c_char_p, c_int, c_void_p]
        self._mpv.mpv_set_property.restype = c_int

        # int mpv_command(mpv_handle *ctx, const char **args);
        self._mpv.mpv_command.argtypes = [c_void_p, POINTER(c_char_p)]
        self._mpv.mpv_command.restype = c_int

        # mpv_wait_event(mpv_handle *ctx, double timeout);
        self._mpv.mpv_wait_event.argtypes = [c_void_p, c_double]
        self._mpv.mpv_wait_event.restype = POINTER(mpv_event)

        # --- render API ---
        # int mpv_render_context_create(mpv_render_context **res, mpv_handle *mpv, mpv_render_param *params);
        self._mpv.mpv_render_context_create.argtypes = [POINTER(c_void_p), c_void_p, POINTER(mpv_render_param)]
        self._mpv.mpv_render_context_create.restype = c_int

        # void mpv_render_context_free(mpv_render_context *ctx);
        self._mpv.mpv_render_context_free.argtypes = [c_void_p]
        self._mpv.mpv_render_context_free.restype = None

        # void mpv_render_context_set_update_callback(mpv_render_context *ctx, mpv_render_update_fn cb, void *cb_ctx);
        self._mpv.mpv_render_context_set_update_callback.argtypes = [c_void_p, mpv_render_update_fn, c_void_p]
        self._mpv.mpv_render_context_set_update_callback.restype = None

        # uint64_t mpv_render_context_update(mpv_render_context *ctx);
        self._mpv.mpv_render_context_update.argtypes = [c_void_p]
        self._mpv.mpv_render_context_update.restype = c_uint64

        # int mpv_render_context_render(mpv_render_context *ctx, mpv_render_param *params);
        self._mpv.mpv_render_context_render.argtypes = [c_void_p, POINTER(mpv_render_param)]
        self._mpv.mpv_render_context_render.restype = c_int

        # void mpv_render_context_report_swap(mpv_render_context *ctx);
        self._mpv.mpv_render_context_report_swap.argtypes = [c_void_p]
        self._mpv.mpv_render_context_report_swap.restype = None

    # -------------
    # Public API
    # -------------

    def initialize(self) -> None:
        """Create mpv handle, init core, and create an OpenGL render context."""
        if self._handle:
            return

        self._handle = c_void_p(self._mpv.mpv_create())
        if not self._handle:
            raise RuntimeError("mpv_create() failed")

        # Critical: prevent mpv from opening vo=gpu/drm/sdl, which fights SDL/KMS.
        self._set_opt("vo", "libmpv")

        # Make mpv quiet & kiosk-friendly.
        self._set_opt("terminal", "no")
        self._set_opt("load-scripts", "no")  # Otherwise it loads a bunch of lua scripts
        self._set_opt("config", "no")  # Ignore ~/.config/mpv/mpv.conf
        self._set_opt("input-default-bindings", "no")
        self._set_opt("osc", "no")

        # Remove OSD clutter.
        self._set_opt("osd-level", "0")

        # Configure log level and logfile.
        self._set_opt("log-file", "/tmp/mpv.log")
        self._set_opt("msg-level", "all=warn")
        # self._set_opt("msg-level", "all=no")

        # Optional. Might help present frames more predictably.
        self._set_opt("video-sync", "display-resample")
        self._set_opt("interpolation", "no")  # Can toggle if necessary. Keeping it simple ('no') for now.

        # Helpful defaults for your kiosk use-case.
        self._set_opt("keep-open", "yes")
        self._set_opt("idle", "yes")

        # Pi/KMS friendliness
        self._set_opt("gpu-api", "opengl")
        self._set_opt("opengl-es", "yes")
        self._set_opt("hwdec", "no")
        self._set_opt("vd-lavc-dr", "no")

        # YouTube: depends on build/config; harmless if unused.
        self._set_opt("ytdl", "yes")

        rc = self._mpv.mpv_initialize(self._handle)
        if rc < 0:
            raise RuntimeError(f"mpv_initialize() failed rc={rc}")

        # Build render context params (OpenGL backend).
        api_type = c_char_p(MPV_RENDER_API_TYPE_OPENGL)

        init_params = mpv_opengl_init_params(
            get_proc_address=self._cb_get_proc,
            get_proc_address_ctx=None,
            extra_exts=None,
        )

        advanced = c_int(1)

        params = (mpv_render_param * 3)(
            mpv_render_param(MPV_RENDER_PARAM_API_TYPE, ctypes.cast(api_type, c_void_p)),
            mpv_render_param(MPV_RENDER_PARAM_OPENGL_INIT_PARAMS, ctypes.cast(byref(init_params), c_void_p)),
            #            mpv_render_param(MPV_RENDER_PARAM_ADVANCED_CONTROL, ctypes.cast(byref(advanced), c_void_p)),
            mpv_render_param(MPV_RENDER_PARAM_INVALID, None),
        )

        out_ctx = c_void_p(None)
        rc = self._mpv.mpv_render_context_create(byref(out_ctx), self._handle, params)
        if rc < 0 or not out_ctx:
            raise RuntimeError(f"mpv_render_context_create() failed rc={rc}")

        self._render_ctx = out_ctx

        # Register update callback ASAP. mpv will invoke it immediately once set.
        self._mpv.mpv_render_context_set_update_callback(self._render_ctx, self._cb_update, None)

    def set_property_flag(self, name: str, value: bool) -> int:
        v = ctypes.c_int(1 if value else 0)
        rc = self._mpv.mpv_set_property(
            self._handle,
            name.encode("utf-8"),
            MPV_FORMAT_FLAG,
            ctypes.byref(v),
        )
        print(f"MPV set_property_flag: {name}={value} rc={rc}")
        return rc

    def report_swap(self) -> None:
        self._mpv.mpv_render_context_report_swap(self._render_ctx)

    def shutdown(self) -> None:
        """Free render context and destroy mpv core."""
        if self._render_ctx:
            try:
                self._mpv.mpv_render_context_set_update_callback(self._render_ctx, mpv_render_update_fn(0), None)
            except Exception:
                pass
            self._mpv.mpv_render_context_free(self._render_ctx)
            self._render_ctx = c_void_p(None)

        if self._handle:
            self._mpv.mpv_terminate_destroy(self._handle)
            self._handle = c_void_p(None)

        print("MPV shutdown complete.")

    def loadfile(self, url: str) -> None:
        """Play a URL (e.g., YouTube) inside the embedded renderer."""
        self.initialize()
        self._command("loadfile", url, "replace")

    def show_text(self, text: str, duration_ms: int = 1000) -> None:
        """Display mpv OSD text (great for a volume overlay)."""
        # show-text: args are (text, duration-ms[, level])
        self._command("show-text", text, str(duration_ms))

    def add_volume(self, volume: int) -> None:
        """Adjust volume and show an overlay."""
        self._command("add", "volume", str(volume))
        # ${volume} expands inside mpv’s OSD text
        self.show_text(f"    Vol: ${volume}%", 800)

    def maybe_render(self, w: int, h: int, force: bool = False) -> bool:
        """
        Return true of mpv drew a new frame into the backbuffer. False otherwise.
        """
        if not self._render_ctx:
            return False

        flags = int(self._mpv.mpv_render_context_update(self._render_ctx))
        want = (flags & MPV_RENDER_UPDATE_FRAME) != 0

        if not want and not force:
            return False

        if self._gl:
            self._gl.glViewport(0, 0, w, h)

        fbo = mpv_opengl_fbo(fbo=0, w=w, h=h, internal_format=0)
        flip_y = c_int(1)

        render_params = (mpv_render_param * 3)(
            mpv_render_param(MPV_RENDER_PARAM_OPENGL_FBO, ctypes.cast(byref(fbo), c_void_p)),
            mpv_render_param(MPV_RENDER_PARAM_FLIP_Y, ctypes.cast(byref(flip_y), c_void_p)),
            mpv_render_param(MPV_RENDER_PARAM_INVALID, None),
        )

        rc = self._mpv.mpv_render_context_render(self._render_ctx, render_params)
        return rc >= 0

    def poll_events(self) -> None:
        """Optional: drain mpv events (not required for playback, but useful for debugging)."""
        if not self._handle:
            return
        while True:
            evp = self._mpv.mpv_wait_event(self._handle, 0.0)
            if not evp:
                break
            ev = evp.contents
            if ev.event_id == 0:  # MPV_EVENT_NONE
                break

    # -------------
    # Internals
    # -------------

    def _set_opt(self, name: str, value: str) -> None:
        rc = self._mpv.mpv_set_option_string(self._handle, name.encode("utf-8"), value.encode("utf-8"))
        # ignore rc for “best-effort” options; you can assert if you prefer

    def _command(self, *args: str) -> None:
        """Call mpv_command with a NULL-terminated argv."""
        if not self._handle:
            raise RuntimeError("MPV not initialized")

        argv = (c_char_p * (len(args) + 1))()
        for i, a in enumerate(args):
            argv[i] = a.encode("utf-8")
        argv[len(args)] = None

        rc = self._mpv.mpv_command(self._handle, argv)
        print(f"MPV command: {args}. Error code: {rc}")
        # rc < 0 indicates an error; for a starter kit we keep it simple

    def _on_mpv_update(self, _ctx: c_void_p) -> None:
        # IMPORTANT: don't call mpv APIs here. Just signal your main loop.
        self._update_event.set()

    def _get_proc_address(self, _ctx: c_void_p, name: bytes) -> c_void_p:
        """
        mpv calls this to resolve OpenGL function pointers.

        We try SDL_GL_GetProcAddress first (since pygame uses SDL2),
        then fall back to eglGetProcAddress if available.
        """
        if self._sdl is not None:
            try:
                self._sdl.SDL_GL_GetProcAddress.argtypes = [c_char_p]
                self._sdl.SDL_GL_GetProcAddress.restype = c_void_p
                p = self._sdl.SDL_GL_GetProcAddress(name)
                if p:
                    return p
            except Exception:
                pass

        if self._egl is not None:
            try:
                self._egl.eglGetProcAddress.argtypes = [c_char_p]
                self._egl.eglGetProcAddress.restype = c_void_p
                p = self._egl.eglGetProcAddress(name)
                if p:
                    return p
            except Exception:
                pass

        return c_void_p(None)


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
