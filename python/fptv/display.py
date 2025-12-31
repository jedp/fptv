"""
Display: owns pygame display, fonts, overlays, and menu rendering.

Coordinates all visual output and flip/swap timing.
"""
import os

import pygame

from fptv.log import Logger
from fptv.render import (
    GLMenuRenderer, OverlayManager, init_viewport, clear_screen,
    draw_menu_surface, make_text_overlay, make_volume_overlay,
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
    - GLMenuRenderer (menu screen)
    - OverlayManager (channel name, volume HUD)
    - Flip/swap coordination

    Usage:
        display = Display(tuner)
        display.initialize()

        # In mainloop:
        display.render_video(tuner_status, force_flip=False)
        # or
        display.render_menu()

        # Overlays:
        display.show_channel_name("PBS", seconds=3.0)
        display.show_volume(75)
    """

    def __init__(self, tuner: Tuner):
        self._tuner = tuner
        self._log = Logger("display")

        self.w = 0
        self.h = 0

        # Initialized in initialize()
        self._font_title: pygame.font.Font | None = None
        self._font_item: pygame.font.Font | None = None
        self._font_small: pygame.font.Font | None = None
        self._renderer: GLMenuRenderer | None = None
        self._overlays: OverlayManager | None = None
        self._menu_surface: pygame.Surface | None = None

    def initialize(self, fullscreen: bool = True) -> None:
        """
        Initialize pygame display, fonts, and renderers.

        Must be called before any rendering.
        """
        pygame.init()
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
            make_text=make_text_overlay,
            make_volume=make_volume_overlay,
        )

        # Menu surface (reused each frame)
        self._menu_surface = pygame.Surface((self.w, self.h))

        self._log.out("Display initialized")

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
        init_viewport(self.w, self.h)
        clear_screen()

        # Render video frame
        did_render = self._tuner.render_frame(self.w, self.h)

        # Tick and draw overlays
        self._overlays.tick()
        self._overlays.draw()

        # Present if we have new content
        if did_render or force_flip:
            pygame.display.flip()
            self._tuner.report_swap()
            return True, did_render

        return False, did_render

    def render_menu(self, subtitle: str = "Press button to toggle video") -> None:
        """
        Render menu screen and present.

        Always flips - menu is not frame-rate sensitive like video.
        """
        init_viewport(self.w, self.h)

        draw_menu_surface(self._menu_surface, self._font_item, subtitle)
        self._renderer.update_from_surface(self._menu_surface)

        clear_screen()
        self._renderer.draw_fullscreen()
        pygame.display.flip()
        self._tuner.report_swap()

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
