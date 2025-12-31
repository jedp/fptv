import ctypes
import time
from dataclasses import dataclass
from typing import Optional, Callable, Dict, Tuple

import pygame

from fptv.gl import GL
from fptv.gl import compile_shader, link_program

GL_COLOR_BUFFER_BIT = 0x00004000
GL_TRIANGLE_STRIP = 0x0005
GL_FLOAT = 0x1406
GL_FALSE = 0
GL_TEXTURE_2D = 0x0DE1
GL_RGBA = 0x1908
GL_UNSIGNED_BYTE = 0x1401
GL_TEXTURE0 = 0x84C0

GL_TEXTURE_MIN_FILTER = 0x2801
GL_TEXTURE_MAG_FILTER = 0x2800
GL_LINEAR = 0x2601
GL_TEXTURE_WRAP_S = 0x2802
GL_TEXTURE_WRAP_T = 0x2803
GL_CLAMP_TO_EDGE = 0x812F

GL_BLEND = 0x0BE2
GL_SRC_ALPHA = 0x0302
GL_ONE_MINUS_SRC_ALPHA = 0x0303

GL_VERTEX_SHADER = 0x8B31
GL_FRAGMENT_SHADER = 0x8B30
GL_ARRAY_BUFFER = 0x8892
GL_STATIC_DRAW = 0x88E4

GL_SCISSOR_TEST = 0x0C11
GL_DEPTH_TEST = 0x0B71
GL_CULL_FACE = 0x0B44

FG_NORM = (220, 220, 220)
FG_SEL = (0, 0, 0)
BG_NORM = (0, 0, 0)
BG_SEL = (90, 105, 255)
FG_INACT = (180, 180, 180)
FG_ACT = (0, 0, 0)
BG_INACT = (0, 0, 0)
BG_ACT = (90, 105, 255)
FG_ALERT = (255, 40, 40)
FG_ACCENT_BLUE = (90, 105, 255)
FG_ACCENT_YELLOW = (220, 150, 0)


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

        vs = compile_shader(vs_src, GL_VERTEX_SHADER)
        fs = compile_shader(fs_src, GL_FRAGMENT_SHADER)
        self.prog = link_program(vs, fs)

        GL.glUseProgram(self.prog)
        self.loc_pos = GL.glGetAttribLocation(self.prog, b"a_pos")
        self.loc_uv = GL.glGetAttribLocation(self.prog, b"a_uv")
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
            GL.glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE,
                            ctypes.cast(buf, ctypes.c_void_p))
            self.tex_w, self.tex_h = w, h
        else:
            GL.glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, w, h, GL_RGBA, GL_UNSIGNED_BYTE,
                               ctypes.cast(buf, ctypes.c_void_p))

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
            x0, y1, 0.0, 0.0,  # bottom-left
            x1, y1, 1.0, 0.0,  # bottom-right
            x0, y0, 0.0, 1.0,  # top-left
            x1, y0, 1.0, 1.0,  # top-right
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
        # GLES 3.x
        vs_src = """
            #version 300 es
            in vec2 a_pos;
            in vec2 a_uv;
            out vec2 v_uv;
            void main() {
                v_uv = a_uv;
                gl_Position = vec4(a_pos, 0.0, 1.0);
            }
            """
        fs_src = """
            #version 300 es
            precision mediump float;
            uniform sampler2D u_tex;
            in vec2 v_uv;
            out vec4 outColor;
            void main() {
                outColor = texture(u_tex, v_uv);
            }
            """

        try:
            vs = compile_shader(vs_src, GL_VERTEX_SHADER)
            fs = compile_shader(fs_src, GL_FRAGMENT_SHADER)
            self.prog = link_program(vs, fs)
        except Exception as e:
            raise RuntimeError(f"Could not compile/link GLES 3.x shader pair: {e}")

        GL.glUseProgram(self.prog)
        self.loc_pos = GL.glGetAttribLocation(self.prog, b"a_pos")
        self.loc_uv = GL.glGetAttribLocation(self.prog, b"a_uv")
        self.loc_tex = GL.glGetUniformLocation(self.prog, b"u_tex")

        # Fullscreen quad (triangle strip): pos(x,y), uv(u,v)
        # Note: UV assumes surface bytes are "RGBA" with top-left origin;
        # we flip the Y in the bytes conversion to match GL's bottom-left.
        verts = (ctypes.c_float * 16)(
            -1.0, -1.0, 0.0, 0.0,
            1.0, -1.0, 1.0, 0.0,
            -1.0, 1.0, 0.0, 1.0,
            1.0, 1.0, 1.0, 1.0,
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


@dataclass
class OverlaySlot:
    quad: "GLOverlayQuad"
    x: int
    y: int
    visible: bool = True
    expires_at: Optional[float] = None  # None = persistent
    content_key: Optional[Tuple] = None

    def set_visible_for(self, seconds: float) -> bool:
        new_expires = time.time() + seconds
        # abs(... ) > 1e-3 avoids "always changed" due to tiny float differences
        changed = (not self.visible) or (self.expires_at is None) or (abs(self.expires_at - new_expires) > 1e-3)
        self.visible = True
        self.expires_at = new_expires
        return changed

    def set_persistent(self) -> bool:
        changed = (not self.visible) or (self.expires_at is not None)
        self.visible = True
        self.expires_at = None
        return changed

    def hide(self) -> bool:
        changed = self.visible or (self.expires_at is not None)
        self.visible = False
        self.expires_at = None
        return changed

    def tick(self, now: float) -> bool:
        if self.expires_at is not None and now >= self.expires_at:
            # This is a real visual change: overlay disappears.
            self.visible = False
            self.expires_at = None
            return True
        return False


class OverlayManager:
    """
    Creates and draws overlays. Caches the last "content_key" per overlay slot
    so we only regenerate the pygame Surface + upload GL texture when needed.
    """

    def __init__(
            self,
            screen_w: int,
            screen_h: int,
            font: pygame.font.Font,
            make_text: Callable[[pygame.font.Font, str], pygame.Surface],
            make_volume: Callable[[pygame.font.Font, int], pygame.Surface],
    ) -> None:
        self.w = screen_w
        self.h = screen_h
        self.font = font
        self._make_text = make_text
        self._make_volume = make_volume
        self._dirty = False

        # Slots
        self.channel = OverlaySlot(
            quad=GLOverlayQuad(screen_w, screen_h),
            x=20,
            y=18,
            visible=False,
        )
        self.volume = OverlaySlot(
            quad=GLOverlayQuad(screen_w, screen_h),
            x=20,
            y=screen_h - 90,
            visible=False,
        )

        # Optionally cache surfaces by key too (useful if you flip back/forth).
        # For your case, caching just the last key per slot is enough, but this
        # is handy for repeated channel names etc.
        self._surface_cache: Dict[Tuple, pygame.Surface] = {}

    def _get_surface_cached(self, key: Tuple, make_fn: Callable[[], pygame.Surface]) -> pygame.Surface:
        s = self._surface_cache.get(key)
        if s is None:
            s = make_fn()
            self._surface_cache[key] = s
        return s

    def set_channel_name(self, name: str, *, seconds: Optional[float] = None) -> bool:
        key = ("channel", name)
        changed = False

        if key != self.channel.content_key:
            surf = self._get_surface_cached(key, lambda: self._make_text(self.font, name))
            self.channel.quad.update_from_surface(surf)
            self.channel.content_key = key
            changed = True

        if seconds is None:
            vis_changed = self.channel.set_persistent()
        else:
            vis_changed = self.channel.set_visible_for(seconds)

        dirty = changed or vis_changed
        self._dirty |= dirty
        return dirty

    def bump_volume(self, vol: int, *, seconds: float = 1.2) -> None:
        """
        Timed volume HUD. Only re-uploads when volume changes.
        """
        vol = int(max(0, min(100, vol)))
        key = ("volume", vol)
        if key != self.volume.content_key:
            surf = self._get_surface_cached(key, lambda: self._make_volume(self.font, vol))
            self.volume.quad.update_from_surface(surf)
            self.volume.content_key = key

        self.volume.set_visible_for(seconds)

        self._dirty = True

    def consume_dirty(self) -> bool:
        dirty = self._dirty
        self._dirty = False
        return dirty

    # ---- per-frame ----

    def tick(self) -> None:
        now = time.time()
        changed = False
        changed |= self.channel.tick(now)
        changed |= self.volume.tick(now)

        self._dirty |= changed

    def draw(self) -> None:
        # Draw in your preferred order (channel first, then volume on top)
        if self.channel.visible:
            self.channel.quad.draw(self.channel.x, self.channel.y)
        if self.volume.visible:
            self.volume.quad.draw(self.volume.x, self.volume.y)


# ----------------------------
# Demo: Toggle MENU <-> VIDEO on Event.PRESS
# ----------------------------

def draw_menu_surface(surf: pygame.Surface, font: pygame.font.Font, subtitle: str) -> None:
    surf.fill((20, 20, 20, 255))
    title = font.render("FPTV MENU", True, (255, 255, 255))
    surf.blit(title, (40, 40))
    sub = font.render(subtitle, True, (200, 200, 200))
    surf.blit(sub, (40, 140))


def init_viewport(w: int, h: int) -> None:
    GL.glViewport(0, 0, w, h)


def clear_screen() -> None:
    GL.glClearColor(0.0, 0.0, 0.0, 1.0)
    GL.glClear(GL_COLOR_BUFFER_BIT)


# -----------------------------------------------------------------------------
# Screen drawing functions
# -----------------------------------------------------------------------------

def draw_main_menu(
        surface: pygame.Surface,
        title_font: pygame.font.Font,
        item_font: pygame.font.Font,
        items: list[str],
        selected: int,
) -> None:
    """Draw the main menu with FPTV title and selectable options."""
    surface.fill(BG_NORM)

    # Title: "FP" in yellow, "TV" in blue
    text_fp = title_font.render("FP", True, FG_ACCENT_YELLOW)
    text_tv = title_font.render("TV", True, FG_ACCENT_BLUE)
    x, y = 60, 40
    surface.blit(text_fp, (x, y))
    surface.blit(text_tv, (x + text_fp.get_width(), y))

    # Menu items
    start_y = 180
    line_h = 70
    pad_x = 60

    for i, text in enumerate(items):
        is_sel = (i == selected)
        bg_color = BG_SEL if is_sel else BG_NORM
        fg_color = FG_SEL if is_sel else FG_NORM

        item_y = start_y + i * line_h
        rect = pygame.Rect(0, item_y, surface.get_width(), line_h)
        pygame.draw.rect(surface, bg_color, rect)

        text_surf = item_font.render(text, True, fg_color)
        text_rect = text_surf.get_rect(midleft=(pad_x, item_y + line_h // 2))
        surface.blit(text_surf, text_rect)


def draw_browse(
        surface: pygame.Surface,
        item_font: pygame.font.Font,
        channels: list,  # List[Channel]
        selected: int,
) -> None:
    """Draw the channel browser with scrolling list."""
    surface.fill(BG_NORM)

    # Header
    header_font = item_font
    header = header_font.render("Channels", True, FG_ACCENT_BLUE)
    surface.blit(header, (20, 10))

    if not channels:
        # No channels message
        msg = item_font.render("No channels found", True, FG_ALERT)
        msg_rect = msg.get_rect(center=(surface.get_width() // 2, surface.get_height() // 2))
        surface.blit(msg, msg_rect)
        return

    # Calculate visible window
    h = surface.get_height()
    header_h = 70
    line_h = 52
    visible = max(1, (h - header_h) // line_h)
    total = len(channels)

    # Simple scroll: selection can move freely in visible area,
    # window scrolls only when selection would go off-screen
    if total <= visible:
        start = 0
    else:
        # Scroll when selection reaches bottom of visible area
        start = max(0, selected - visible + 1)
        # Don't scroll past the end
        start = min(start, total - visible)
    
    end = min(start + visible, total)

    y0 = header_h
    for row, idx in enumerate(range(start, end)):
        channel = channels[idx]
        is_sel = (idx == selected)
        fg_color = FG_SEL if is_sel else FG_NORM
        bg_color = BG_SEL if is_sel else BG_NORM

        item_y = y0 + row * line_h
        rect = pygame.Rect(0, item_y, surface.get_width(), line_h)
        pygame.draw.rect(surface, bg_color, rect)

        # Channel name (short name like "7.1 KQED")
        text_surf = item_font.render(channel.name, True, fg_color)
        text_rect = text_surf.get_rect(midleft=(20, item_y + line_h // 2))
        surface.blit(text_surf, text_rect)


def draw_about(
        surface: pygame.Surface,
        title_font: pygame.font.Font,
        item_font: pygame.font.Font,
        info: dict[str, str],
) -> None:
    """Draw the about screen with device information."""
    surface.fill(BG_NORM)

    # Title
    title = title_font.render("About", True, FG_ACCENT_BLUE)
    surface.blit(title, (60, 40))

    # Info lines
    y = 160
    line_h = 50

    for key, value in info.items():
        # Key in dim color, value in bright
        key_surf = item_font.render(f"{key}:", True, FG_INACT)
        val_surf = item_font.render(value, True, FG_NORM)

        surface.blit(key_surf, (40, y))
        surface.blit(val_surf, (40 + key_surf.get_width() + 20, y))
        y += line_h


def draw_scan(
        surface: pygame.Surface,
        title_font: pygame.font.Font,
        item_font: pygame.font.Font,
        status: str = "Not implemented yet",
) -> None:
    """Draw the scan screen (placeholder for now)."""
    surface.fill(BG_NORM)

    title = title_font.render("Scan", True, FG_ACCENT_BLUE)
    surface.blit(title, (60, 40))

    msg = item_font.render(status, True, FG_NORM)
    msg_rect = msg.get_rect(center=(surface.get_width() // 2, surface.get_height() // 2))
    surface.blit(msg, msg_rect)


# -----------------------------------------------------------------------------
# Legacy/reference code (commented)
# -----------------------------------------------------------------------------

"""
def draw_menu(surface, title_font, item_font,
              items: List[str], selected: int):
    surface.fill((0, 0, 0))
    text_fp = title_font.render("FP", True, FG_ACCENT_YELLOW)
    text_tv = title_font.render("TV", True, FG_ACCENT_BLUE)
    x, y = 60, 60
    surface.blit(text_fp, (x, y))
    surface.blit(text_tv, (x + text_fp.get_width(), y))

    start_y = 200
    line_h = 70
    line_w = surface.get_width()

    for i, text in enumerate(items):
        is_sel = (i == selected)
        bg_color = BG_SEL if is_sel else BG_NORM
        fg_color = FG_SEL if is_sel else FG_NORM

        y = start_y + i * line_h
        rect = pygame.Rect(x, y, line_w, line_h)
        pygame.draw.rect(surface, bg_color, rect)

        text_surf = item_font.render(text, True, fg_color)
        text_rect = text_surf.get_rect()
        text_rect.midleft = (x, y + line_h // 2)

        surface.blit(text_surf, text_rect)


def draw_browse(surface, item_font,
                channels: List[Channel], selected: int):
    surface.fill(BG_NORM)
    header = "Back"
    fg_color = BG_SEL if selected == -1 else FG_NORM
    bg_color = BG_NORM

    img = item_font.render(header, True, fg_color, bg_color)
    surface.blit(img, (20, 0))

    if not channels:
        draw_centered_text(
            surface, item_font, "No channels", surface.get_height() // 2,
            color=FG_ALERT)
        return

    # Show a window around selection
    h = surface.get_height()
    visible = max(5, (h - 148) // 52)
    half = visible // 2
    start = max(0, selected - half)
    end = min(len(channels), start + visible)
    start = max(0, end - visible)

    y0 = 130
    line_h = 52
    line_w = surface.get_width()
    for row, idx in enumerate(range(start, end)):
        text = channels[idx].name
        is_sel = (idx == selected)
        fg_color = FG_SEL if is_sel else FG_NORM
        bg_color = BG_SEL if is_sel else BG_NORM
        y = y0 + row * line_h
        rect = pygame.Rect(0, y, line_w, line_h)
        pygame.draw.rect(surface, bg_color, rect)
        text_surf = item_font.render(text, True, fg_color)
        text_rect = text_surf.get_rect()
        text_rect.midleft = (20, y + line_h // 2)
        surface.blit(text_surf, text_rect)


def draw_playing(surface, title_font, item_font, small_font, name: str):
    surface.fill((0, 0, 0))
    img = small_font.render("Press to stop and return", True, FG_NORM)
    surface.blit(img, (20, 20))
    draw_centered_text(surface, title_font, "Playing", 90)
    draw_centered_text(surface, item_font, name, 190)


def draw_escaping(surface, large_font, small_font):
    surface.fill((0, 0, 0))
    text_title = large_font.render("Escape the Package!", True, FG_ALERT)
    surface.blit(text_title, (20, 20))
    msg = small_font.render("If you're confused, press the power button.", True, FG_NORM)
    surface.blit(msg, (20, 100))


def draw_centered_text(surface, font, text, y, color=FG_NORM):
    img = font.render(text, True, color)
    r = img.get_rect(center=(surface.get_width() // 2, y))
    surface.blit(img, r)
"""
