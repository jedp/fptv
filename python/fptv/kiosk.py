#!/usr/bin/env python3
import os
from dataclasses import dataclass
from enum import Enum, auto
from queue import SimpleQueue
from typing import List

import pygame

from fptv.hw import HwEventBinding
from fptv.input import Action, InputMapper
from fptv.log import Logger
from fptv.render import GLMenuRenderer, OverlayManager, init_viewport
from fptv.render import draw_menu_surface, make_text_overlay, make_volume_overlay, clear_screen
from fptv.tuner import Tuner, TunerState
from fptv.tvh import Channel, TVHeadendScanner, ScanConfig

FPTV_CAPTION = "fptv"
ASSETS_FONT = os.path.join(os.path.dirname(os.path.realpath(__file__)), "assets/fonts")

SCREEN_H = 480
SCREEN_W = 800


class Screen(Enum):
    MENU = auto()
    BROWSE = auto()
    TUNE = auto()
    PLAY = auto()
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
    def __init__(self, screen_w: int = SCREEN_W, screen_h: int = SCREEN_H):
        self.w = screen_w
        self.h = screen_h
        self.log = Logger("fptv")
        self._event_queue = SimpleQueue()
        self.tvh = TVHeadendScanner(ScanConfig.from_env())
        self.hw = HwEventBinding(self._event_queue)
        self.input = InputMapper(self._event_queue)
        self.state = State(channels=self.tvh.get_playlist_channels())

        pygame.init()
        pygame.font.init()

        self._init_renderer()
        self.tuner = Tuner(self.tvh)
        self.tuner.initialize()

        self.renderer = GLMenuRenderer(self.w, self.h)

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

    def mainloop(self) -> None:
        pygame.init()
        pygame.display.set_mode((self.w, self.h), pygame.FULLSCREEN | pygame.OPENGL | pygame.DOUBLEBUF)

        menu_surf = pygame.Surface((SCREEN_W, SCREEN_H))

        # Channel list
        self.log.out(f"Loaded {len(self.state.channels)} channels")

        mode: Screen = Screen.MENU
        self.overlays.set_channel_name("Channel: None")
        force_flip = False

        running = True
        clock = pygame.time.Clock()

        while running:
            clock.tick(60)

            # --- Handle input actions ---
            for action in self.input.poll():
                if action == Action.QUIT:
                    running = False

                elif action == Action.TOGGLE_MODE:
                    if mode == Screen.MENU:
                        # Start video mode
                        self.tuner.resume()
                        if self.state.channels:
                            ch = self.state.channels[self.state.browse_index]
                            self.tuner.tune_now(ch.url, f"Channel: {ch.name}")
                            self.overlays.set_channel_name(f"Channel: {ch.name}", seconds=3.0)
                            mode = Screen.TUNE
                        else:
                            mode = Screen.PLAY
                        force_flip = True
                    else:
                        # Return to menu
                        mode = Screen.MENU
                        self.tuner.cancel()
                        self.tuner.pause()
                        force_flip = True

                elif action in (Action.NEXT_CHANNEL, Action.PREV_CHANNEL):
                    if not self.state.channels:
                        continue

                    delta = 1 if action == Action.NEXT_CHANNEL else -1
                    i = self.state.browse_index + delta
                    i = max(0, min(len(self.state.channels) - 1, i))
                    self.state.browse_index = i
                    channel = self.state.channels[self.state.browse_index]
                    self.overlays.set_channel_name(f"Channel: {channel.name}", seconds=3.0)
                    force_flip = True

                    # If in video mode, request debounced tune
                    if mode in (Screen.PLAY, Screen.TUNE):
                        self.tuner.request_tune(channel.url, f"Channel: {channel.name}")

                elif action == Action.VOLUME_UP:
                    self.tuner.add_volume(5)

                elif action == Action.VOLUME_DOWN:
                    self.tuner.add_volume(-5)

            # --- Render ---
            init_viewport(self.w, self.h)

            if mode in (Screen.PLAY, Screen.TUNE):
                clear_screen()

                # Render video frame, then tick tuner state machine
                did_render = self.tuner.render_frame(self.w, self.h)
                tune_status = self.tuner.tick(did_render)

                # Update mode based on tuner state
                if tune_status.state == TunerState.PLAYING:
                    mode = Screen.PLAY
                elif tune_status.state == TunerState.TUNING:
                    mode = Screen.TUNE
                elif tune_status.state == TunerState.FAILED:
                    self.overlays.set_channel_name("No signal", seconds=3.0)
                    self.tuner.pause()
                    mode = Screen.MENU
                    force_flip = True

                # Show status messages (Retrying…, etc.)
                if tune_status.message:
                    seconds = 5.0 if tune_status.message == "Retrying…" else 3.0
                    self.overlays.set_channel_name(tune_status.message, seconds=seconds)
                    force_flip = True

                # Overlays
                self.overlays.tick()
                self.overlays.draw()

                # Present
                if did_render or force_flip:
                    pygame.display.flip()
                    self.tuner.report_swap()
                    force_flip = False

            else:
                # MENU: draw menu and present
                draw_menu_surface(menu_surf, self.font_item, "Press button to toggle video")
                self.renderer.update_from_surface(menu_surf)

                clear_screen()
                self.renderer.draw_fullscreen()
                pygame.display.flip()
                self.tuner.report_swap()

        self.shutdown()

    def shutdown(self) -> int:
        try:
            print("Releasing GPIOs.")
            self.hw.close()
            print("Shutting down tuner.")
            self.tuner.shutdown()
            print("Quitting pygame engine.")
            pygame.quit()
        except Exception as e:
            print(f"Error during shutdown: {e}")
            return -1

        print("Bye!")
        return 0


if __name__ == "__main__":
    raise SystemExit(FPTV().mainloop())
