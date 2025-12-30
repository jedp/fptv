import ctypes
import threading
import time
from ctypes import (
    c_void_p, c_char_p, c_int, c_double, c_uint64,
    POINTER, Structure, CFUNCTYPE, byref
)
from ctypes.util import find_library

from fptv.gl import mpv_opengl_get_proc_address_fn
from fptv.log import Logger

MPV_SOCK = "/tmp/fptv-mpv.sock"

# mpv_format enum values (from mpv/client.h)
MPV_FORMAT_NONE = 0
MPV_FORMAT_STRING = 1
MPV_FORMAT_OSD_STRING = 2
MPV_FORMAT_FLAG = 3
MPV_FORMAT_INT64 = 4
MPV_FORMAT_DOUBLE = 5
MPV_FORMAT_NODE = 6
MPV_FORMAT_NODE_ARRAY = 7
MPV_FORMAT_NODE_MAP = 8
MPV_FORMAT_BYTE_ARRAY = 9

MPV_USERAGENT = "fptv/embedded-mpv"

# mpv_render_param_type values (from render.h)
MPV_RENDER_PARAM_INVALID = 0
MPV_RENDER_PARAM_API_TYPE = 1
MPV_RENDER_PARAM_OPENGL_INIT_PARAMS = 2
MPV_RENDER_PARAM_OPENGL_FBO = 3
MPV_RENDER_PARAM_FLIP_Y = 4
MPV_RENDER_PARAM_ADVANCED_CONTROL = 10

# Predefined API type string (from render.h)
MPV_OPT_RENDER_API_TYPE_OPENGL = "opengl"

# mpv_render_update_flag values
MPV_RENDER_UPDATE_FRAME = 1 << 00

MPV_DEBOUNCE_PLAY_S = 0.150
MPV_MIN_SWITCH_GAP_S = 0.35  # min time between real loadfile calls
MPV_STOP_SETTLE_s = 0.25  # pause after stop to let server notice close
MPV_OPT_NETWORK_TIMEOUT_S = 30

MPV_FLAG_PAUSE = "pause"

class MPVError(RuntimeError):
    pass


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
        self.log = Logger("mpv")
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

        self._pending_url: str | None = None
        self._current_url: str | None = None
        self._switch_after = 0.0
        self._switch_inflight_until = 0.0

        self._stage: str | None = None  # None | 'stop_wait'
        self._stop_until = 0.0
        self._next_url: str | None = None

        # tune these
        self._debounce_s = MPV_DEBOUNCE_PLAY_S
        self._min_switch_gap_s = MPV_MIN_SWITCH_GAP_S
        self._stop_settle_s = MPV_DEBOUNCE_PLAY_S

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

        # int mpv_get_property(mpv_handle *ctx, const char *name, mpv_format format, void *data);
        self._mpv.mpv_get_property.argtypes = [c_void_p, c_char_p, c_int, c_void_p]
        self._mpv.mpv_get_property.restype = c_int

        # char *mpv_get_property_string(mpv_handle *ctx, const char *name);
        self._mpv.mpv_get_property_string.argtypes = [c_void_p, c_char_p]
        self._mpv.mpv_get_property_string.restype = c_void_p  # returns char* you must mpv_free()

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

        # Tag all our requests.
        self._set_opt("user-agent", MPV_USERAGENT)
        self._set_opt("network-timeout", str(str(MPV_OPT_NETWORK_TIMEOUT_S)))

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
        self._set_opt("keep-open", "no")
        self._set_opt("idle", "yes")

        # Pi/KMS friendliness
        self._set_opt("gpu-api", MPV_OPT_RENDER_API_TYPE_OPENGL)
        self._set_opt("opengl-es", "yes")
        self._set_opt("hwdec", "no")
        self._set_opt("vd-lavc-dr", "no")

        # YouTube: depends on build/config; harmless if unused.
        self._set_opt("ytdl", "no")

        rc = self._mpv.mpv_initialize(self._handle)
        if rc < 0:
            raise RuntimeError(f"mpv_initialize() failed rc={rc}")

        # Build render context params (OpenGL backend).
        api_type = c_char_p(MPV_OPT_RENDER_API_TYPE_OPENGL.encode("utf-8"))

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

    def pause(self) -> int:
        return self._set_property_flag(MPV_FLAG_PAUSE.encode("utf-8"), True)

    def resume(self) -> int:
        return self._set_property_flag(MPV_FLAG_PAUSE.encode("utf-8"), False)

    def is_paused(self) -> bool:
        return self._get_property_flag(MPV_FLAG_PAUSE.encode("utf-8"))

    def stop(self):
        self._exec("stop")

    def report_swap(self) -> None:
        self._mpv.mpv_render_context_report_swap(self._render_ctx)

    def tick(self) -> bool:
        """
        Call every frame.
        Returns True if we *initiated* a tune (either stop or loadfile).

        This is intentionally non-blocking (no sleep), so the render loop can keep
        calling mpv_render_context_render() regularly.
        """
        now = time.time()

        # Stage 2: we already issued stop; wait a short settle window, then load.
        if self._stage == "stop_wait":
            if now < self._stop_until:
                return False
            url = self._next_url
            self._next_url = None
            self._stage = None

            if not url or url == self._current_url:
                return False

            self._exec("loadfile", url, "replace")
            self._set_property_flag(MPV_FLAG_PAUSE.encode("utf-8"), False)
            self._current_url = url
            # prevent immediate re-tune storms
            self._switch_inflight_until = now + self._min_switch_gap_s
            return True

        # Nothing queued.
        if not self._pending_url:
            return False

        # Debounce/coalesce rapid selection changes.
        if now < self._switch_after:
            return False

        # Enforce a minimum gap between tune attempts.
        if now < self._switch_inflight_until:
            return False

        url = self._pending_url
        self._pending_url = None

        # no-op if it's already playing this url
        if url == self._current_url:
            return False

        # Stage 1: stop, then let the HTTP connection close a moment.
        self._exec("stop")
        self._stage = "stop_wait"
        self._next_url = url
        self._stop_until = now + self._stop_settle_s

        # Reserve the "inflight" window starting now (includes settle time).
        self._switch_inflight_until = now + self._min_switch_gap_s
        return True

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
        """Coalesce rapid requests; latest wins."""
        self.initialize()
        self._pending_url = url
        self._switch_after = time.time() + self._debounce_s

    def loadfile_now(self, url: str) -> None:
        """Queue a tune immediately (no debounce). Useful for watchdog recovery."""
        self.initialize()
        self._pending_url = url
        self._switch_after = 0.0

    def show_text(self, text: str, duration_ms: int = 1000) -> None:
        """Display mpv OSD text (great for a volume overlay)."""
        # show-text: args are (text, duration-ms[, level])
        self._exec("show-text", text, str(duration_ms))

    def add_volume(self, volume: int) -> None:
        """Adjust volume and show an overlay."""
        self._exec("add", "volume", str(volume))
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

    def _set_opt(self, name: str, value: str) -> int:
        rc = self._mpv.mpv_set_option_string(self._handle, name.encode("utf-8"), value.encode("utf-8"))
        # ignore rc for “best-effort” options; you can assert if you prefer
        return rc

    def _get_property_flag(self, name: bytes) -> bool:
        v = c_int()  # FLAG uses int (0/1)
        err = self._mpv.mpv_get_property(self._handle, name, MPV_FORMAT_FLAG, byref(v))
        if err < 0 or err > 1:
            self.log.err(f"mpv_get_property('pause') failed: {err}")
        return bool(v.value)

    def _set_property_flag(self, name: bytes, value: bool) -> int:
        v = ctypes.c_int(1 if value else 0)
        rc = self._mpv.mpv_set_property(self._handle, name, MPV_FORMAT_FLAG, byref(v))
        print(f"MPV set_property_flag: {name}={value} rc={rc}")
        return rc

    def _exec(self, *args: str) -> int:
        if not self._handle:
            raise RuntimeError("MPV not initialized")

        argv = (c_char_p * (len(args) + 1))()
        for i, a in enumerate(args):
            argv[i] = a.encode("utf-8")
        argv[len(args)] = None

        rc = self._mpv.mpv_command(self._handle, argv)
        print(f"MPV command: {args}. Error code: {rc}")
        return rc

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
