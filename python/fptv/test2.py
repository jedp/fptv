#!/usr/bin/env python3
import ctypes
from ctypes.util import find_library
from dataclasses import dataclass
from queue import SimpleQueue, Empty
import time

import pygame

# Your project imports (adjust paths/names as needed)
from fptv.event import Event
from fptv.hw import FPTVHW           # or whatever produces Event.PRESS/ROT_L/ROT_R
from fptv.test import EmbeddedMPV    # replace with your module path

MPV_FORMAT_FLAG = 3

# ----------------------------
# Minimal OpenGL/GLES bindings via ctypes
# ----------------------------

def _load_gl():
    # On Pi/KMS this is often GLESv2; fallback to desktop GL.
    for name in ("GLESv2", "GL"):
        path = find_library(name)
        if path:
            try:
                return ctypes.CDLL(path)
            except OSError:
                pass
    raise RuntimeError("Could not load GLESv2 or GL")

GL = _load_gl()

# Constants we need
GL_COLOR_BUFFER_BIT = 0x00004000
GL_TRIANGLE_STRIP   = 0x0005
GL_FLOAT            = 0x1406
GL_FALSE            = 0
GL_TEXTURE_2D       = 0x0DE1
GL_RGBA             = 0x1908
GL_UNSIGNED_BYTE    = 0x1401
GL_TEXTURE0         = 0x84C0

GL_TEXTURE_MIN_FILTER = 0x2801
GL_TEXTURE_MAG_FILTER = 0x2800
GL_LINEAR             = 0x2601
GL_TEXTURE_WRAP_S      = 0x2802
GL_TEXTURE_WRAP_T      = 0x2803
GL_CLAMP_TO_EDGE       = 0x812F

GL_BLEND               = 0x0BE2
GL_SRC_ALPHA           = 0x0302
GL_ONE_MINUS_SRC_ALPHA = 0x0303

GL_VERTEX_SHADER   = 0x8B31
GL_FRAGMENT_SHADER = 0x8B30
GL_COMPILE_STATUS  = 0x8B81
GL_LINK_STATUS     = 0x8B82
GL_INFO_LOG_LENGTH = 0x8B84
GL_ARRAY_BUFFER    = 0x8892
GL_STATIC_DRAW     = 0x88E4

GL_SCISSOR_TEST = 0x0C11
GL_DEPTH_TEST   = 0x0B71
GL_CULL_FACE    = 0x0B44

GL.glDisable.argtypes = [ctypes.c_uint]
GL.glDisable.restype = None

# Function signatures (just what we use)
GL.glViewport.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
GL.glViewport.restype = None

GL.glClearColor.argtypes = [ctypes.c_float, ctypes.c_float, ctypes.c_float, ctypes.c_float]
GL.glClearColor.restype = None

GL.glClear.argtypes = [ctypes.c_uint]
GL.glClear.restype = None

GL.glEnable.argtypes = [ctypes.c_uint]
GL.glEnable.restype = None

GL.glBlendFunc.argtypes = [ctypes.c_uint, ctypes.c_uint]
GL.glBlendFunc.restype = None

GL.glActiveTexture.argtypes = [ctypes.c_uint]
GL.glActiveTexture.restype = None

GL.glGenTextures.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint)]
GL.glGenTextures.restype = None

GL.glBindTexture.argtypes = [ctypes.c_uint, ctypes.c_uint]
GL.glBindTexture.restype = None

GL.glTexParameteri.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_int]
GL.glTexParameteri.restype = None

GL.glTexImage2D.argtypes = [
    ctypes.c_uint, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p
]
GL.glTexImage2D.restype = None

GL.glTexSubImage2D.argtypes = [
    ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p
]
GL.glTexSubImage2D.restype = None

GL.glDrawArrays.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_int]
GL.glDrawArrays.restype = None

# Shader/program functions
GL.glCreateShader.argtypes = [ctypes.c_uint]
GL.glCreateShader.restype = ctypes.c_uint

GL.glShaderSource.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.POINTER(ctypes.c_char_p), ctypes.POINTER(ctypes.c_int)]
GL.glShaderSource.restype = None

GL.glCompileShader.argtypes = [ctypes.c_uint]
GL.glCompileShader.restype = None

GL.glGetShaderiv.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_int)]
GL.glGetShaderiv.restype = None

GL.glGetShaderInfoLog.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.c_void_p]
GL.glGetShaderInfoLog.restype = None

GL.glCreateProgram.argtypes = []
GL.glCreateProgram.restype = ctypes.c_uint

GL.glAttachShader.argtypes = [ctypes.c_uint, ctypes.c_uint]
GL.glAttachShader.restype = None

GL.glLinkProgram.argtypes = [ctypes.c_uint]
GL.glLinkProgram.restype = None

GL.glGetProgramiv.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_int)]
GL.glGetProgramiv.restype = None

GL.glGetProgramInfoLog.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.c_void_p]
GL.glGetProgramInfoLog.restype = None

GL.glUseProgram.argtypes = [ctypes.c_uint]
GL.glUseProgram.restype = None

GL.glGetAttribLocation.argtypes = [ctypes.c_uint, ctypes.c_char_p]
GL.glGetAttribLocation.restype = ctypes.c_int

GL.glGetUniformLocation.argtypes = [ctypes.c_uint, ctypes.c_char_p]
GL.glGetUniformLocation.restype = ctypes.c_int

GL.glUniform1i.argtypes = [ctypes.c_int, ctypes.c_int]
GL.glUniform1i.restype = None

# VBO + vertex attribs
GL.glGenBuffers.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint)]
GL.glGenBuffers.restype = None

GL.glBindBuffer.argtypes = [ctypes.c_uint, ctypes.c_uint]
GL.glBindBuffer.restype = None

GL.glBufferData.argtypes = [ctypes.c_uint, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_uint]
GL.glBufferData.restype = None

GL.glEnableVertexAttribArray.argtypes = [ctypes.c_uint]
GL.glEnableVertexAttribArray.restype = None

GL.glVertexAttribPointer.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_uint, ctypes.c_ubyte, ctypes.c_int, ctypes.c_void_p]
GL.glVertexAttribPointer.restype = None


def _compile_shader(src: str, shader_type: int) -> int:
    sh = GL.glCreateShader(shader_type)
    src_b = src.encode("utf-8")
    src_p = ctypes.c_char_p(src_b)
    length = ctypes.c_int(len(src_b))
    GL.glShaderSource(sh, 1, ctypes.byref(src_p), ctypes.byref(length))
    GL.glCompileShader(sh)

    ok = ctypes.c_int(0)
    GL.glGetShaderiv(sh, GL_COMPILE_STATUS, ctypes.byref(ok))
    if not ok.value:
        log_len = ctypes.c_int(0)
        GL.glGetShaderiv(sh, GL_INFO_LOG_LENGTH, ctypes.byref(log_len))
        buf = ctypes.create_string_buffer(log_len.value or 4096)
        GL.glGetShaderInfoLog(sh, len(buf), None, buf)
        raise RuntimeError("Shader compile failed:\n" + buf.value.decode("utf-8", "replace"))
    return sh


def _link_program(vs: int, fs: int) -> int:
    prog = GL.glCreateProgram()
    GL.glAttachShader(prog, vs)
    GL.glAttachShader(prog, fs)
    GL.glLinkProgram(prog)

    ok = ctypes.c_int(0)
    GL.glGetProgramiv(prog, GL_LINK_STATUS, ctypes.byref(ok))
    if not ok.value:
        log_len = ctypes.c_int(0)
        GL.glGetProgramiv(prog, GL_INFO_LOG_LENGTH, ctypes.byref(log_len))
        buf = ctypes.create_string_buffer(max(1, log_len.value))
        GL.glGetProgramInfoLog(prog, len(buf), None, buf)
        raise RuntimeError(f"Program link failed:\n{buf.value.decode('utf-8', 'replace')}")
    return prog


# ----------------------------
# GL texture quad renderer for a pygame.Surface
# ----------------------------

class GLOverlayQuad:
    """
    Upload a pygame Surface into a GL texture and draw it as a quad at a pixel position.
    Designed for small overlays (text, volume HUD), not full-screen UI.
    """
    def __init__(self, screen_w: int, screen_h: int):
        self.screen_w = screen_w
        self.screen_h = screen_h

        vs_src = """#version 300 es
        precision mediump float;
        layout(location=0) in vec2 a_pos;
        layout(location=1) in vec2 a_uv;
        out vec2 v_uv;
        void main() {
            v_uv = a_uv;
            gl_Position = vec4(a_pos, 0.0, 1.0);
        }
        """

        fs_src = """#version 300 es
        precision mediump float;
        uniform sampler2D u_tex;
        in vec2 v_uv;
        out vec4 outColor;
        void main() {
            outColor = texture(u_tex, v_uv);
        }
        """

        vs = _compile_shader(vs_src, GL_VERTEX_SHADER)
        fs = _compile_shader(fs_src, GL_FRAGMENT_SHADER)
        self.prog = _link_program(vs, fs)

        GL.glUseProgram(self.prog)
        self.loc_pos = GL.glGetAttribLocation(self.prog, b"a_pos")
        self.loc_uv  = GL.glGetAttribLocation(self.prog, b"a_uv")
        self.loc_tex = GL.glGetUniformLocation(self.prog, b"u_tex")
        GL.glUniform1i(self.loc_tex, 0)

        # VBO (we'll rewrite it each draw)
        vbo = ctypes.c_uint(0)
        GL.glGenBuffers(1, ctypes.byref(vbo))
        self.vbo = vbo.value

        # Texture (allocated on first update)
        tex = ctypes.c_uint(0)
        GL.glGenTextures(1, ctypes.byref(tex))
        self.tex = tex.value

        GL.glActiveTexture(GL_TEXTURE0)
        GL.glBindTexture(GL_TEXTURE_2D, self.tex)
        GL.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
        GL.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
        GL.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
        GL.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)

        self.tex_w = 0
        self.tex_h = 0

        # Ensure blending is on for alpha overlays
        GL.glEnable(GL_BLEND)
        GL.glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)

    def update_from_surface(self, surf: pygame.Surface) -> None:
        """
        Upload RGBA pixels from pygame surface into the GL texture.
        For overlays, keep surf small to avoid bandwidth.
        """
        w, h = surf.get_width(), surf.get_height()
        rgba = pygame.image.tostring(surf, "RGBA", True)  # flip_y=True
        buf = ctypes.create_string_buffer(rgba)

        GL.glActiveTexture(GL_TEXTURE0)
        GL.glBindTexture(GL_TEXTURE_2D, self.tex)

        if (w, h) != (self.tex_w, self.tex_h):
            # Allocate storage
            GL.glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE, ctypes.cast(buf, ctypes.c_void_p))
            self.tex_w, self.tex_h = w, h
        else:
            GL.glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, w, h, GL_RGBA, GL_UNSIGNED_BYTE, ctypes.cast(buf, ctypes.c_void_p))

    def draw(self, x: int, y: int, w: int | None = None, h: int | None = None) -> None:
        """
        Draw the overlay at (x,y) in pixels (top-left origin), scaled to (w,h) if provided.
        If w/h omitted, uses texture dimensions.
        """
        if self.tex_w == 0 or self.tex_h == 0:
            return

        if w is None: w = self.tex_w
        if h is None: h = self.tex_h

        # Convert pixel rect -> clip space (-1..1), with y down
        x0 = (x / self.screen_w) * 2.0 - 1.0
        x1 = ((x + w) / self.screen_w) * 2.0 - 1.0
        y0 = 1.0 - (y / self.screen_h) * 2.0
        y1 = 1.0 - ((y + h) / self.screen_h) * 2.0

        # Triangle strip: (pos.xy, uv.xy)
        verts = (ctypes.c_float * 16)(
            x0, y1,  0.0, 0.0,  # bottom-left
            x1, y1,  1.0, 0.0,  # bottom-right
            x0, y0,  0.0, 1.0,  # top-left
            x1, y0,  1.0, 1.0,  # top-right
        )

        # mpv may leave GL state changed; put it in a known-good state for overlays
        GL.glDisable(GL_SCISSOR_TEST)
        GL.glDisable(GL_DEPTH_TEST)
        GL.glDisable(GL_CULL_FACE)
        GL.glEnable(GL_BLEND)
        GL.glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)

        GL.glUseProgram(self.prog)

        GL.glBindBuffer(GL_ARRAY_BUFFER, self.vbo)
        GL.glBufferData(GL_ARRAY_BUFFER, ctypes.sizeof(verts), ctypes.cast(verts, ctypes.c_void_p), GL_STATIC_DRAW)

        stride = 4 * 4
        GL.glEnableVertexAttribArray(self.loc_pos)
        GL.glVertexAttribPointer(self.loc_pos, 2, GL_FLOAT, 0, stride, ctypes.c_void_p(0))
        GL.glEnableVertexAttribArray(self.loc_uv)
        GL.glVertexAttribPointer(self.loc_uv, 2, GL_FLOAT, 0, stride, ctypes.c_void_p(2 * 4))

        GL.glActiveTexture(GL_TEXTURE0)
        GL.glBindTexture(GL_TEXTURE_2D, self.tex)

        GL.glDrawArrays(GL_TRIANGLE_STRIP, 0, 4)

def make_text_overlay(font: pygame.font.Font, text: str) -> pygame.Surface:
    pad = 16
    fg = (255, 255, 255)
    bg = (0, 0, 0, 160)  # translucent

    text_s = font.render(text, True, fg)
    w, h = text_s.get_width() + pad * 2, text_s.get_height() + pad * 2
    surf = pygame.Surface((w, h), pygame.SRCALPHA, 32).convert_alpha()
    surf.fill((0, 0, 0, 0))
    pygame.draw.rect(surf, bg, surf.get_rect(), border_radius=14)
    surf.blit(text_s, (pad, pad))
    return surf


def make_volume_overlay(font: pygame.font.Font, vol: int) -> pygame.Surface:
    # vol is 0..100
    w, h = 360, 70
    pad = 12
    surf = pygame.Surface((w, h), pygame.SRCALPHA, 32).convert_alpha()
    surf.fill((0, 0, 0, 0))

    pygame.draw.rect(surf, (0, 0, 0, 160), surf.get_rect(), border_radius=16)

    label = font.render(f"Vol {vol:3d}%", True, (255, 255, 255))
    surf.blit(label, (pad, 10))

    bar_x, bar_y = pad, 42
    bar_w, bar_h = w - pad * 2, 16
    pygame.draw.rect(surf, (255, 255, 255, 50), (bar_x, bar_y, bar_w, bar_h), border_radius=8)

    fill_w = int(bar_w * max(0, min(100, vol)) / 100)
    pygame.draw.rect(surf, (255, 255, 255, 220), (bar_x, bar_y, fill_w, bar_h), border_radius=8)

    return surf



@dataclass
class GLMenuRenderer:
    w: int
    h: int

    def __post_init__(self):
        # Try GLES3 shader first, then GLES2, then desktop-ish
        shader_pairs = [
            # GLES 3.x
            ("""
            #version 300 es
            in vec2 a_pos;
            in vec2 a_uv;
            out vec2 v_uv;
            void main() {
                v_uv = a_uv;
                gl_Position = vec4(a_pos, 0.0, 1.0);
            }
            """,
             """
            #version 300 es
            precision mediump float;
            uniform sampler2D u_tex;
            in vec2 v_uv;
            out vec4 outColor;
            void main() {
                outColor = texture(u_tex, v_uv);
            }
            """),
            # GLES 2.x
            ("""
            attribute vec2 a_pos;
            attribute vec2 a_uv;
            varying vec2 v_uv;
            void main() {
                v_uv = a_uv;
                gl_Position = vec4(a_pos, 0.0, 1.0);
            }
            """,
             """
            precision mediump float;
            uniform sampler2D u_tex;
            varying vec2 v_uv;
            void main() {
                gl_FragColor = texture2D(u_tex, v_uv);
            }
            """),
            # Desktop GL 3.1-ish
            ("""
            #version 130
            in vec2 a_pos;
            in vec2 a_uv;
            out vec2 v_uv;
            void main() {
                v_uv = a_uv;
                gl_Position = vec4(a_pos, 0.0, 1.0);
            }
            """,
             """
            #version 130
            uniform sampler2D u_tex;
            in vec2 v_uv;
            out vec4 outColor;
            void main() {
                outColor = texture(u_tex, v_uv);
            }
            """),
        ]

        last_err = None
        for vs_src, fs_src in shader_pairs:
            try:
                vs = _compile_shader(vs_src, GL_VERTEX_SHADER)
                fs = _compile_shader(fs_src, GL_FRAGMENT_SHADER)
                self.prog = _link_program(vs, fs)
                break
            except Exception as e:
                last_err = e
                self.prog = 0
        if not self.prog:
            raise RuntimeError(f"Could not compile/link any shader pair. Last error: {last_err}")

        GL.glUseProgram(self.prog)
        self.loc_pos = GL.glGetAttribLocation(self.prog, b"a_pos")
        self.loc_uv  = GL.glGetAttribLocation(self.prog, b"a_uv")
        self.loc_tex = GL.glGetUniformLocation(self.prog, b"u_tex")

        # Fullscreen quad (triangle strip): pos(x,y), uv(u,v)
        # Note: UV assumes your surface bytes are "RGBA" with top-left origin;
        # we flip the Y in the bytes conversion to match GL's bottom-left.
        verts = (ctypes.c_float * 16)(
            -1.0, -1.0,  0.0, 0.0,
             1.0, -1.0,  1.0, 0.0,
            -1.0,  1.0,  0.0, 1.0,
             1.0,  1.0,  1.0, 1.0,
        )

        vbo = ctypes.c_uint(0)
        GL.glGenBuffers(1, ctypes.byref(vbo))
        self.vbo = vbo.value
        GL.glBindBuffer(GL_ARRAY_BUFFER, self.vbo)
        GL.glBufferData(GL_ARRAY_BUFFER, ctypes.sizeof(verts), ctypes.cast(verts, ctypes.c_void_p), GL_STATIC_DRAW)

        # Create texture
        tex = ctypes.c_uint(0)
        GL.glGenTextures(1, ctypes.byref(tex))
        self.tex = tex.value
        GL.glActiveTexture(GL_TEXTURE0)
        GL.glBindTexture(GL_TEXTURE_2D, self.tex)
        GL.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
        GL.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
        GL.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
        GL.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)

        # Allocate empty texture storage once
        GL.glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, self.w, self.h, 0, GL_RGBA, GL_UNSIGNED_BYTE, None)

        # Blending for alpha UI
        GL.glEnable(GL_BLEND)
        GL.glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)

        # Hook texture unit 0 to u_tex
        GL.glUseProgram(self.prog)
        GL.glUniform1i(self.loc_tex, 0)

    def update_from_surface(self, surf: pygame.Surface) -> None:
        """Upload pygame surface pixels into the GL texture."""
        # Convert surface to RGBA bytes; flip vertically so it appears correctly.
        rgba = pygame.image.tostring(surf, "RGBA", True)
        buf = ctypes.create_string_buffer(rgba)

        GL.glActiveTexture(GL_TEXTURE0)
        GL.glBindTexture(GL_TEXTURE_2D, self.tex)
        GL.glTexSubImage2D(
            GL_TEXTURE_2D, 0,
            0, 0, self.w, self.h,
            GL_RGBA, GL_UNSIGNED_BYTE,
            ctypes.cast(buf, ctypes.c_void_p)
        )

    def draw_fullscreen(self) -> None:
        """Draw the texture as a fullscreen quad."""
        GL.glUseProgram(self.prog)
        GL.glBindBuffer(GL_ARRAY_BUFFER, self.vbo)

        stride = 4 * 4  # 4 floats per vertex (pos2 + uv2)
        # a_pos at offset 0
        GL.glEnableVertexAttribArray(self.loc_pos)
        GL.glVertexAttribPointer(self.loc_pos, 2, GL_FLOAT, 0, stride, ctypes.c_void_p(0))
        # a_uv at offset 8 bytes (2 floats)
        GL.glEnableVertexAttribArray(self.loc_uv)
        GL.glVertexAttribPointer(self.loc_uv, 2, GL_FLOAT, 0, stride, ctypes.c_void_p(2 * 4))

        GL.glActiveTexture(GL_TEXTURE0)
        GL.glBindTexture(GL_TEXTURE_2D, self.tex)

        GL.glDrawArrays(GL_TRIANGLE_STRIP, 0, 4)


# ----------------------------
# Demo: Toggle MENU <-> VIDEO on Event.PRESS
# ----------------------------

def draw_menu_surface(surf: pygame.Surface, font: pygame.font.Font, subtitle: str) -> None:
    surf.fill((20, 20, 20, 255))
    title = font.render("FPTV MENU", True, (255, 255, 255))
    surf.blit(title, (40, 40))
    sub = font.render(subtitle, True, (200, 200, 200))
    surf.blit(sub, (40, 140))


def main():
    event_queue: SimpleQueue[Event] = SimpleQueue()
    hw = FPTVHW(event_queue)

    pygame.init()
    pygame.font.init()

    info = pygame.display.Info()
    w, h = info.current_w, info.current_h
    print("SDL driver:", pygame.display.get_driver())

    pygame.display.set_mode((w, h), pygame.OPENGL | pygame.DOUBLEBUF | pygame.FULLSCREEN)
    pygame.mouse.set_visible(False)

    # Prepare menu surface + GL uploader
    menu_surf = pygame.Surface((w, h), flags=pygame.SRCALPHA, depth=32).convert_alpha()
    font = pygame.font.Font(None, 72)
    menu_renderer = GLMenuRenderer(w, h)

    # mpv embedded
    mpv = EmbeddedMPV()
    mpv.initialize()
    mpv.loadfile("av://lavfi:mandelbrot")  # easy test source
    mpv.show_text("Video ready", 800)

    mode = "MENU"  # start on menu, but keep mpv alive
    mpv.set_property_flag("pause", True)

    clock = pygame.time.Clock()
    running = True

    # Setup once:
    overlay_channel = GLOverlayQuad(w, h)
    overlay_vol     = GLOverlayQuad(w, h)
    font = pygame.font.Font(None, 42)

    # TODO
    # When you pick it up tomorrow, a couple of “next steps” that usually go
    # smoothly in this setup:
    #
    # - Make a tiny OverlayManager that caches textures for things that change
    #   rarely (channel name) and only re-uploads when text changes.
    #
    # - Keep volume overlay as a timed HUD (vol_until = now + 1.2) and just
    #   redraw/upload on knob turns.
    #
    # - If you see any weird overlay glitches after mpv draws, it’s almost always
    #   GL state — keep that little “reset state” block right before drawing
    #   overlays.
    #
    # If you run into anything odd while integrating into the kiosk
    # (mode switching, event loop pacing, or mpv lifetimes),
    # paste the relevant slice and I’ll help you tighten it up.

    while running:
        # Drain input
        try:
            while True:
                ev = event_queue.get_nowait()

                if ev == Event.PRESS:
                    # Toggle
                    if mode == "MENU":
                        mode = "VIDEO"
                        mpv.set_property_flag("pause", False)
                        mpv.show_text("Video", 600)
                    else:
                        mode = "MENU"
                        mpv.set_property_flag("pause", True)
                        mpv.show_text("Menu", 600)

                elif ev in (Event.ROT_R, Event.ROT_L):
                    # On channel change
                    #channel_text = f"Channel: {ch.name}"
                    #overlay_channel.update_from_surface(make_text_overlay(font, channel_text))
                    #mpv.loadfile(ch.url)
                    delta = 1 if ev == Event.ROT_R else -1
                    vol_value = max(0, min(100, vol_value + delta))
                    overlay_vol.update_from_surface(make_volume_overlay(font, vol_value))
                    vol_until = time.time() + 1.2
                    mpv.set_property("volume", vol_value)  # or mpv.command("set_property", "volume", str(vol_value))


        except Empty:
            pass

        channel_text = "Channel: 7.1 PBS"
        overlay_channel.update_from_surface(make_text_overlay(font, channel_text))

        vol_value = 25
        overlay_vol.update_from_surface(make_volume_overlay(font, vol_value))
        vol_until = 0.0  # hide unless recently changed


        # Render
        GL.glViewport(0, 0, w, h)

        if mode == "VIDEO":
            # Clear so the backbuffer is deterministic
            GL.glClearColor(0.0, 0.0, 0.0, 1.0)
            GL.glClear(GL_COLOR_BUFFER_BIT)

            # Render video
            did_render = mpv.maybe_render(w, h)

            # Draw overlays on top of video
            # (Channel name persistent)
            overlay_channel.draw(x=20, y=18)

            # (Volume overlay only for ~1.2s after a change)
            if time.time() < vol_until:
                overlay_vol.draw(x=20, y=screen_h - 90)

            # If you ever pause video and still want overlays to appear immediately, the clean approach is:
            # when you update an overlay, set a flag force_one_flip=True
            if did_render:
                pygame.display.flip()
                mpv._mpv.mpv_render_context_report_swap(mpv.render_ctx)
            else:
                # If mpv produced no new frame, don't flip (avoids buffer ping-pong flicker)
                time.sleep(0.002)

        else:
            # MENU: draw menu into texture and present
            draw_menu_surface(menu_surf, font, "Press button to toggle video")
            menu_renderer.update_from_surface(menu_surf)

            GL.glClearColor(0.05, 0.05, 0.05, 1.0)
            GL.glClear(GL_COLOR_BUFFER_BIT)
            menu_renderer.draw_fullscreen()

            pygame.display.flip()

        clock.tick(60)

    hw.close()
    mpv.shutdown()
    pygame.quit()


if __name__ == "__main__":
    main()

