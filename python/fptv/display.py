"""
Display: owns pygame display, fonts, overlays, and menu rendering.

Coordinates all visual output and flip/swap timing.
"""
import os

import pygame

from fptv.log import Logger
from fptv.render import (
    GLMenuRenderer, OverlayManager, init_viewport, clear_screen,
    make_text_overlay, make_volume_overlay,
    draw_main_menu, draw_browse, draw_about, draw_scan,
)
from fptv.tuner import Tuner

ASSETS_FONT = os.path.join(os.path.dirname(os.path.realpath(__file__)), "assets/fonts")

PI_DISPLAY_W = 800
PI_DISPLAY_H = 480


class Display:
    """
    Owns all rendering concerns:
    - pygame display initialization
    - Fonts
    - GLMenuRenderer (menu screens)
    - OverlayManager (channel name, volume HUD)
    - Flip/swap coordination

    Usage:
        display = Display()
        tuner = Tuner(tvh)  # needs GL context from display
        display.set_tuner(tuner)

        # Render different screens:
        display.render_main_menu(items, selected)
        display.render_browse(channels, selected)
        display.render_about(info)
        display.render_video(force_flip)

        # Overlays:
        display.show_channel_name("PBS", seconds=3.0)
        display.show_volume(75)
    """

    def __init__(self, fullscreen: bool = True):
        self._tuner: Tuner | None = None
        self._log = Logger("display")
        self._menu_key: tuple | None = None  # What the menu last drew

        self._initialize(fullscreen)

    def _initialize(self, fullscreen: bool) -> None:
        """Initialize pygame display, fonts, and renderers."""
        # Not pygame.init(), which would open the audio device and lock mpv out.
        pygame.display.init()
        pygame.font.init()

        if fullscreen:
            pygame.display.set_mode((0, 0), pygame.OPENGL | pygame.DOUBLEBUF | pygame.FULLSCREEN)
        else:
            pygame.display.set_mode((PI_DISPLAY_W, PI_DISPLAY_H), pygame.OPENGL | pygame.DOUBLEBUF)

        pygame.mouse.set_visible(False)

        self.w, self.h = pygame.display.get_surface().get_size()
        self._log.out(f"SDL driver: {pygame.display.get_driver()} size={self.w}x{self.h}")

        # Fonts
        self._font_title = pygame.font.Font(f"{ASSETS_FONT}/VeraSeBd.ttf", 92)
        self._font_item = pygame.font.Font(f"{ASSETS_FONT}/VeraSe.ttf", 56)
        self._font_small = pygame.font.Font(f"{ASSETS_FONT}/VeraSe.ttf", 32)

        # Menu renderer
        self._renderer = GLMenuRenderer(self.w, self.h)

        # Overlays
        self._overlays = OverlayManager(
            screen_w=self.w, screen_h=self.h,
            font=self._font_item,
            volume_font=self._font_small,
            make_text=make_text_overlay,
            make_volume=make_volume_overlay,
        )

        # Menu surface (reused each frame)
        self._menu_surface = pygame.Surface((self.w, self.h))

        self._log.out("Display initialized")

    def set_tuner(self, tuner: Tuner) -> None:
        """Set the tuner reference. Must be called before render_video()."""
        self._tuner = tuner

    # -------------------------------------------------------------------------
    # Rendering
    # -------------------------------------------------------------------------

    def render_video(self, force_flip: bool = False) -> tuple[bool, bool]:
        """
        Render video frame with overlays.

        Args:
            force_flip: Force a flip even if no new video frame

        Returns:
            (did_flip, did_render_frame) tuple.
            - did_flip: True if pygame.display.flip() was called
            - did_render_frame: True if mpv rendered a new video frame
        """
        self._menu_key = None
        init_viewport(self.w, self.h)
        clear_screen()

        # Render video frame
        # Forcing redraws the last frame, so that flipping doesn't show black.
        did_render = self._tuner.render_frame(self.w, self.h, force=force_flip)

        # Tick and draw overlays
        self._overlays.tick()
        self._overlays.draw()

        # Present if we have new content
        if did_render or force_flip:
            pygame.display.flip()
            self._tuner.report_swap()
            return True, did_render

        return False, did_render

    def _menu_changed(self, key: tuple) -> bool:
        """
        True if the screen needs redrawing. Uploading a full-screen texture
        is expensive, so menus only draw when their content or overlays change.
        """
        self._overlays.tick()
        overlays_changed = self._overlays.consume_dirty()
        if key == self._menu_key and not overlays_changed:
            return False
        self._menu_key = key
        return True

    def _present_menu(self) -> None:
        """Draw the menu surface and any overlays, and flip."""
        clear_screen()
        self._renderer.draw_fullscreen()
        self._overlays.draw()
        pygame.display.flip()
        self._tuner.report_swap()

    def render_main_menu(self, items: list[str], selected: int) -> None:
        """Render main menu screen with selectable options."""
        if not self._menu_changed(("main", tuple(items), selected)):
            return

        init_viewport(self.w, self.h)

        draw_main_menu(self._menu_surface, self._font_title, self._font_item, items, selected)
        self._renderer.update_from_surface(self._menu_surface)

        self._present_menu()

    def render_browse(self, channels: list, selected: int, epg_map: dict = None) -> None:
        """Render channel browser screen with optional EPG (now playing) info."""
        if not self._menu_changed(("browse", id(channels), selected, id(epg_map))):
            return

        init_viewport(self.w, self.h)

        draw_browse(self._menu_surface, self._font_item, channels, selected, epg_map, self._font_small)
        self._renderer.update_from_surface(self._menu_surface)

        self._present_menu()

    def render_about(self, info: dict[str, str], selected: int = 0) -> None:
        """Render about screen with device info. selected=-1 means Back."""
        if not self._menu_changed(("about", tuple(info.items()), selected)):
            return

        init_viewport(self.w, self.h)

        draw_about(self._menu_surface, self._font_title, self._font_item, info,
                   back_selected=(selected == -1))
        self._renderer.update_from_surface(self._menu_surface)

        self._present_menu()

    def render_scan(self, status: str, selected: int = 0) -> None:
        """Render scan screen. selected=-1 means Back."""
        if not self._menu_changed(("scan", status, selected)):
            return

        init_viewport(self.w, self.h)

        draw_scan(self._menu_surface, self._font_title, self._font_item, status,
                  back_selected=(selected == -1))
        self._renderer.update_from_surface(self._menu_surface)

        self._present_menu()

    # -------------------------------------------------------------------------
    # Overlays
    # -------------------------------------------------------------------------

    def show_channel_name(self, name: str, seconds: float | None = None) -> None:
        """
        Show channel name overlay.

        Args:
            name: Text to display
            seconds: Auto-hide after this many seconds (None = persistent)
        """
        self._overlays.set_channel_name(name, seconds=seconds)

    def show_volume(self, volume: int, seconds: float = 1.2) -> None:
        """Show volume HUD overlay."""
        self._overlays.bump_volume(volume, seconds=seconds)

    def hide_channel_name(self) -> None:
        """Hide the channel name overlay."""
        self._overlays.channel.hide()

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------

    def shutdown(self) -> None:
        """Clean up display resources."""
        self._log.out("Shutting down tuner")
        self._tuner.shutdown()
        self._log.out("Shutting down pygame")
        pygame.quit()
