#!/usr/bin/env python3
import os
import time
from dataclasses import dataclass
from enum import Enum, auto
from queue import SimpleQueue, Empty
from typing import List

import pygame

from fptv.event import Event
from fptv.hw import FPTVHW
from fptv.log import Logger
from fptv.mpv import EmbeddedMPV
from fptv.render import GLMenuRenderer, OverlayManager, init_viewport
from fptv.render import draw_menu_surface, make_text_overlay, make_volume_overlay, clear_screen
from fptv.tvh import Channel, TVHeadendScanner, ScanConfig

MPV_FORMAT_FLAG = 3

FPTV_CAPTION = "fptv"
ASSETS_FONT = os.path.join(os.path.dirname(os.path.realpath(__file__)), "assets/fonts")


class Screen(Enum):
    MENU = auto()
    BROWSE = auto()
    PLAYING = auto()
    SCAN = auto()
    SHUTDOWN = auto()
    MAINTENANCE = auto()


@dataclass
class State:
    screen: Screen = Screen.MENU
    main_index: int = 0  # 0=Browse 1=Scan 2=Shutdown
    browse_index: int = 0
    channels: List[Channel] = None
    playing_name: str = ""
    volume: int = 30

    def __post_init__(self):
        if self.channels is None:
            self.channels = []


class FPTV:
    def __init__(self):
        self.log = Logger("fptv")
        self.event_queue = SimpleQueue()
        self.tvh = TVHeadendScanner(ScanConfig.from_env())
        self.hw = FPTVHW(self.event_queue)
        self.state = State(channels=self.tvh.get_playlist_channels())

        pygame.init()
        pygame.font.init()

        self._init_renderer()  # creates GL context + renderer + overlays
        self.mpv = EmbeddedMPV()
        self.mpv.initialize()  # now GL proc lookup is valid

    def _init_renderer(self):
        pygame.display.set_mode((0, 0), pygame.OPENGL | pygame.DOUBLEBUF | pygame.FULLSCREEN)
        pygame.mouse.set_visible(False)

        w, h = pygame.display.get_surface().get_size()
        self.log.out(f"SDL driver: {pygame.display.get_driver()} size={w}x{h}")

        self.font_title = pygame.font.Font(f"{ASSETS_FONT}/VeraSeBd.ttf", 92)
        self.font_item = pygame.font.Font(f"{ASSETS_FONT}/VeraSe.ttf", 56)
        self.font_small = pygame.font.Font(f"{ASSETS_FONT}/VeraSe.ttf", 32)

        self.renderer = GLMenuRenderer(w, h)
        self.overlays = OverlayManager(
            screen_w=w, screen_h=h,
            font=self.font_item,
            make_text=make_text_overlay,
            make_volume=make_volume_overlay,
        )
        self.log.out("Renderer initialized")

    def mainloop(self) -> int:
        info = pygame.display.Info()
        w, h = info.current_w, info.current_h

        # Prepare menu surface + GL uploader
        menu_surf = pygame.Surface((w, h), flags=pygame.SRCALPHA, depth=32).convert_alpha()

        # mpv embedded
        self.mpv.loadfile("av://lavfi:mandelbrot")  # easy test source
        self.mpv.show_text("Video ready", 800)

        self.mpv.set_property_flag("pause", True)

        clock = pygame.time.Clock()
        running = True

        self.overlays.set_channel_name("Channel: None")

        mode: Screen = Screen.MENU
        while running:
            pygame.event.pump()
            force_flip = False

            try:
                while True:
                    ev = self.event_queue.get_nowait()

                    if ev == Event.PRESS:
                        # Toggle
                        if mode == Screen.MENU:
                            mode = Screen.PLAYING
                            self.mpv.set_property_flag("pause", False)
                            force_flip = True
                        else:
                            mode = Screen.MENU
                            self.mpv.set_property_flag("pause", True)
                            force_flip = True

                    elif ev in (Event.ROT_R, Event.ROT_L):
                        if not self.state.channels:
                            self.log.out("No channels available.")
                            continue

                        i = self.state.browse_index + 1 if ev == Event.ROT_R else self.state.browse_index - 1
                        i = max(0, min(len(self.state.channels) - 1, i))
                        self.state.browse_index = i
                        ch = self.state.channels[self.state.browse_index]
                        self.state.playing_name = ch.name
                        channel_name = f"Channel: {ch.name}"
                        self.overlays.set_channel_name(channel_name)
                        self.mpv.loadfile(ch.url)

                        # Move volume controls to other encoder when it's wired up.
                        # delta = 1 if ev == Event.ROT_R else -1
                        # vol_value = max(0, min(100, vol_value + delta))
                        # overlay_vol.update_from_surface(make_volume_overlay(font_item, vol_value))
                        # vol_until = time.time() + 1.2
                        # mpv.command("set_property", "volume", str(vol_value))


            except Empty:
                pass

            # Render
            init_viewport(w, h)

            if mode == Screen.PLAYING:
                # Clear so the backbuffer is deterministic
                clear_screen()

                # Render video
                did_render = self.mpv.maybe_render(w, h)
                self.overlays.tick()
                self.overlays.draw()

                # If you ever pause video and still want overlays to appear immediately, the clean approach is:
                # when you update an overlay, set a flag force_one_flip=True
                if did_render or force_flip:
                    pygame.display.flip()
                    self.mpv.report_swap()
                else:
                    # If mpv produced no new frame, don't flip (avoids buffer ping-pong flicker)
                    time.sleep(0.002)

            else:
                # MENU: draw menu into texture and present
                draw_menu_surface(menu_surf, self.font_item, "Press button to toggle video")
                self.renderer.update_from_surface(menu_surf)

                clear_screen()
                self.renderer.draw_fullscreen()
                pygame.display.flip()

            clock.tick(60)

        return self.shutdown()

    def shutdown(self) -> int:
        try:
            print("Releasing GPIOs.")
            self.hw.close()
            print("Shutting down player.")
            self.mpv.shutdown()
            print("Quitting pygame engine.")
            pygame.quit()
        except Exception as e:
            print(f"Error during shutdown: {e}")
            return -1

        print("Bye!")
        return 0


if __name__ == "__main__":
    raise SystemExit(FPTV().mainloop())
