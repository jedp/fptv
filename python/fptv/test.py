#!/usr/bin/env python3
"""
embedded_mpv.py — a minimal “starter kit” for embedding mpv into a pygame OpenGL
surface using libmpv's render API.

Goal:
- Run on a console/KMS setup (no X11/Wayland), using pygame's SDL2 OpenGL context.
- Keep mpv *inside* your process (no external mpv subprocess).
- Render mpv frames into the current OpenGL framebuffer each frame.
- Use mpv commands like "show-text" for simple overlays (volume, status).

Notes:
- This uses ctypes to bind libmpv; no third-party Python mpv bindings required.
- For YouTube URLs, mpv typically needs yt-dlp installed (depends on your mpv build/config).
"""

import ctypes
import sys
import threading
from ctypes import (
    c_void_p, c_char_p, c_int, c_double, c_uint64,
    POINTER, Structure, CFUNCTYPE, byref
)
from ctypes.util import find_library
from queue import SimpleQueue, Empty
from OpenGL.GL import glViewport, glClearColor, glClear, GL_COLOR_BUFFER_BIT

import pygame

from fptv.event import Event
from fptv.hw import FPTVHW

MPV_FORMAT_FLAG = 3

# ----------------------------
# Minimal libmpv render bindings
# ----------------------------

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
MPV_RENDER_UPDATE_FRAME = 1 << 0


class mpv_render_param(Structure):
    _fields_ = [
        ("type", c_int),
        ("data", c_void_p),
    ]


# From render_gl.h
# typedef void *(*mpv_opengl_get_proc_address_fn)(void *ctx, const char *name);
mpv_opengl_get_proc_address_fn = CFUNCTYPE(c_void_p, c_void_p, c_char_p)


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


# Optional: minimal mpv_event so we can drain events (not strictly required for playback).
class mpv_event(Structure):
    _fields_ = [
        ("event_id", c_int),
        ("error", c_int),
        ("reply_userdata", c_uint64),
        ("data", c_void_p),
    ]


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
            except Exception:
                self._gl = None

        self.handle = c_void_p(None)
        self.render_ctx = c_void_p(None)

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
        if self.handle:
            return

        self.handle = c_void_p(self._mpv.mpv_create())
        if not self.handle:
            raise RuntimeError("mpv_create() failed")

        # Critical: prevent mpv from opening vo=gpu/drm/sdl, which fights SDL/KMS.
        self._set_opt("vo", "libmpv")

        # Make mpv quiet & kiosk-friendly.
        self._set_opt("terminal", "no")
        self._set_opt("load-scripts", "no") # Otherwise it loads a bunch of lua scripts
        self._set_opt("config", "no") # Ignore ~/.config/mpv/mpv.conf
        self._set_opt("input-default-bindings", "no")
        self._set_opt("osc", "no")

        # Remove OSD clutter.
        self._set_opt("osd-level", "0")

        # Configure log level and logfile.
        self._set_opt("log-file", "/tmp/mpv.log")
        self._set_opt("msg-level", "all=warn")
        #self._set_opt("msg-level", "all=no")

        # Optional. Might help present frames more predictably.
        self._set_opt("video-sync", "display-resample")
        self._set_opt("interpolation", "no") # Can toggle if necessary. Keeping it simple ('no') for now.

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

        rc = self._mpv.mpv_initialize(self.handle)
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
        rc = self._mpv.mpv_render_context_create(byref(out_ctx), self.handle, params)
        if rc < 0 or not out_ctx:
            raise RuntimeError(f"mpv_render_context_create() failed rc={rc}")

        self.render_ctx = out_ctx

        # Register update callback ASAP. mpv will invoke it immediately once set.
        self._mpv.mpv_render_context_set_update_callback(self.render_ctx, self._cb_update, None)

    def set_property_flag(self, name: str, value: bool) -> int:
        v = ctypes.c_int(1 if value else 0)
        rc = self._mpv.mpv_set_property(
            self.handle,
            name.encode("utf-8"),
            MPV_FORMAT_FLAG,
            ctypes.byref(v),
        )
        print(f"MPV set_property_flag: {name}={value} rc={rc}")
        return rc

    def shutdown(self) -> None:
        """Free render context and destroy mpv core."""
        if self.render_ctx:
            try:
                self._mpv.mpv_render_context_set_update_callback(self.render_ctx, mpv_render_update_fn(0), None)
            except Exception:
                pass
            self._mpv.mpv_render_context_free(self.render_ctx)
            self.render_ctx = c_void_p(None)

        if self.handle:
            self._mpv.mpv_terminate_destroy(self.handle)
            self.handle = c_void_p(None)

        print("MPV shutdown complete.")

    def loadfile(self, url: str) -> None:
        """Play a URL (e.g., YouTube) inside the embedded renderer."""
        self.initialize()
        self.command("loadfile", url, "replace")

    def show_text(self, text: str, duration_ms: int = 1000) -> None:
        """Display mpv OSD text (great for a volume overlay)."""
        # show-text: args are (text, duration-ms[, level])
        self.command("show-text", text, str(duration_ms))

    def add_volume(self, delta: int) -> None:
        """Adjust volume and show an overlay."""
        self.command("add", "volume", str(delta))
        # ${volume} expands inside mpv’s OSD text
        self.show_text(f"    Vol: ${volume}%", 800)

    def maybe_render(self, w: int, h: int) -> bool:
        """
        Call this from your main loop. If mpv wants a redraw, render a frame into
        the current OpenGL framebuffer.
        """
        if not self.render_ctx:
            return

        # Debugging; uncomment me later
        #if not self._update_event.is_set():
        #    return

        # If advanced control is enabled, mpv requires you to call update() after each callback.
        # update() returns flags; if MPV_RENDER_UPDATE_FRAME is set, render.
        self._update_event.clear()

        # It's possible multiple callbacks happened; loop until update returns 0.
        did_render = False
        while True:
            flags = int(self._mpv.mpv_render_context_update(self.render_ctx))
            if (flags & MPV_RENDER_UPDATE_FRAME) == 0:
                break

            if self._gl:
                self._gl.glViewport(0, 0, w, h)

            fbo = mpv_opengl_fbo(fbo=0, w=w, h=h, internal_format=0)
            flip_y = c_int(1)  # useful when rendering to the default framebuffer

            rparams = (mpv_render_param * 3)(
                mpv_render_param(MPV_RENDER_PARAM_OPENGL_FBO, ctypes.cast(byref(fbo), c_void_p)),
                mpv_render_param(MPV_RENDER_PARAM_FLIP_Y, ctypes.cast(byref(flip_y), c_void_p)),
                mpv_render_param(MPV_RENDER_PARAM_INVALID, None),
            )

            rc = self._mpv.mpv_render_context_render(self.render_ctx, rparams)
            if rc < 0:
                # If you want: print or log rc
                break

            # Tell mpv we swapped a frame (timing/helpful if used consistently).
            self._mpv.mpv_render_context_report_swap(self.render_ctx)

            # Drain any queued update signals quickly (if one arrived mid-loop).
            if not self._update_event.is_set():
                # If no new callback came in, no need to spin.
                pass

            did_render = True

        return did_render

    def render_if_needed(self, w: int, h: int) -> None:
        if not self.render_ctx:
            return

        flags = int(self._mpv.mpv_render_context_update(self.render_ctx))
        if (flags & MPV_RENDER_UPDATE_FRAME) == 0:
            return

        if self._gl:
            self._gl.glViewport(0, 0, w, h)

        fbo = mpv_opengl_fbo(fbo=0, w=w, h=h, internal_format=0)  # 0 is allowed :contentReference[oaicite:12]{index=12}
        flip_y = c_int(1)

        rparams = (mpv_render_param * 3)(
            mpv_render_param(MPV_RENDER_PARAM_OPENGL_FBO, ctypes.cast(byref(fbo), c_void_p)),
            mpv_render_param(MPV_RENDER_PARAM_FLIP_Y, ctypes.cast(byref(flip_y), c_void_p)),
            mpv_render_param(MPV_RENDER_PARAM_INVALID, None),
        )
        self._mpv.mpv_render_context_render(self.render_ctx, rparams)


    def poll_events(self) -> None:
        """Optional: drain mpv events (not required for playback, but useful for debugging)."""
        if not self.handle:
            return
        while True:
            evp = self._mpv.mpv_wait_event(self.handle, 0.0)
            if not evp:
                break
            ev = evp.contents
            if ev.event_id == 0:  # MPV_EVENT_NONE
                break

    # -------------
    # Internals
    # -------------

    def _set_opt(self, name: str, value: str) -> None:
        rc = self._mpv.mpv_set_option_string(self.handle, name.encode("utf-8"), value.encode("utf-8"))
        # ignore rc for “best-effort” options; you can assert if you prefer

    def command(self, *args: str) -> None:
        """Call mpv_command with a NULL-terminated argv."""
        if not self.handle:
            raise RuntimeError("MPV not initialized")

        argv = (c_char_p * (len(args) + 1))()
        for i, a in enumerate(args):
            argv[i] = a.encode("utf-8")
        argv[len(args)] = None

        rc = self._mpv.mpv_command(self.handle, argv)
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


# ----------------------------
# Demo main: play a YouTube URL
# ----------------------------

def main() -> int:
    # If you want to force console/KMS SDL driver, uncomment:
    # os.environ.setdefault("SDL_VIDEODRIVER", "kmsdrm")

    url = sys.argv[1] if len(sys.argv) > 1 else "av://lavfi:mandelbrot"

    event_queue: SimpleQueue[Event] = SimpleQueue()
    # Setup hardware GPIOs and rotary encoder.
    # Store references to GPIO objects so they don't get garbage collected.
    hw = FPTVHW(event_queue)

    ok, fail = pygame.init()
    print("pygame.init:", ok, "ok,", fail, "failed")
    print("display init:", pygame.display.get_init())
    print("SDL driver:", pygame.display.get_driver())


    info = pygame.display.Info()
    w, h = info.current_w, info.current_h

    # OpenGL fullscreen
    print("Opening OpenGL fullscreen")
    pygame.display.set_mode((w, h), pygame.OPENGL | pygame.DOUBLEBUF | pygame.FULLSCREEN)

    glViewport(0, 0, w, h)
    glClearColor(1.0, 0.0, 1.0, 1.0)
    glClear(GL_COLOR_BUFFER_BIT)
    pygame.display.flip()

    import time
    time.sleep(1.0)

    pygame.display.set_caption("embedded_mpv.py")
    pygame.mouse.set_visible(False)

    mpv = EmbeddedMPV()
    mpv.initialize()
    mpv.loadfile(url)
    mpv.show_text("Loading…", 1200)

    running = True
    clock = pygame.time.Clock()

    # Sequence of procedures:
    # drain input events (rotary/button)
    # render if needed
    # flip only if rendered
    # sleep/tick
    while running:
        try:
            ev = event_queue.get_nowait()

            if ev == Event.PRESS:
                running = False

            elif ev in (Event.ROT_R, Event.ROT_L):
                delta = 1 if ev == Event.ROT_R else -1
                mpv.add_volume(delta)

            # Optional: mpv.poll_events()
        except Empty:
            pass

        if mpv.maybe_render(w, h):
            pygame.display.flip()
            mpv._mpv.mpv_render_context_report_swap(mpv.render_ctx)  # or wrap in mpv.report_swap()
        else:
            # No new frames. Don't flip frame buffer.
            pass

        # Basic pacing (mpv internally times frames; this just keeps CPU sane)
        clock.tick(60)

    hw.close()
    mpv.shutdown()
    pygame.quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
