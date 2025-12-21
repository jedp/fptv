#!/usr/bin/env python3

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from enum import Enum, auto
from queue import SimpleQueue, Empty
from typing import List

import pygame

from event import Event
from hw import setup_encoder
from mpv import MPV
from tvh import Channel, ScanConfig, TVHeadendScanner
from render import draw_menu, draw_browse, draw_playing
from x11 import blanking_disable, blanking_enable, ui_show, ui_hide, mpv_raise, mpv_lower

FPTV_CAPTION = 'fptv'

ASSETS_FONT = os.path.join(os.path.dirname(os.path.realpath(__file__)), "assets/fonts")


class Screen(Enum):
    MAIN = auto()
    BROWSE = auto()
    PLAYING = auto()
    SCAN = auto()  # placeholder
    SHUTDOWN = auto()


@dataclass
class State:
    screen: Screen = Screen.MAIN
    main_index: int = 0  # 0=Browse 1=Scan 2=Shutdown
    browse_index: int = 0
    channels: List[Channel] = None
    playing_name: str = ""

    def __post_init__(self):
        if self.channels is None:
            self.channels = []


class FPTV:
    def __init__(self):
        self.running = False
        self.mpv = MPV()
        self.tvh = TVHeadendScanner(ScanConfig.from_env())
        self.eventQueue: SimpleQueue[Event] = SimpleQueue()
        self.blanking_is_disabled = False

        # Setup hardware GPIOs and rotary encoder.
        # Store references to GPIO objects so they don't get garbage collected.
        self.encoder, self.button = setup_encoder(self.eventQueue)

        os.environ.setdefault("SDL_VIDEO_CENTERED", "1")
        os.environ["SDL_VIDEO_X11_WMCLASS"] = FPTV_CAPTION

        pygame.init()
        pygame.font.init()
        pygame.mouse.set_visible(False)

        self.font_title = pygame.font.Font(f"{ASSETS_FONT}/VeraSeBd.ttf", 92)
        self.font_item = pygame.font.Font(f"{ASSETS_FONT}/VeraSe.ttf", 56)
        self.font_small = pygame.font.Font(f"{ASSETS_FONT}/VeraSe.ttf", 32)


    def _set_blanking(self, disable: bool):
        if disable and not self.blanking_is_disabled:
            blanking_disable()
            self.blanking_is_disabled = True
        elif (not disable) and self.blanking_is_disabled:
            blanking_enable()
            self.blanking_is_disabled = False

    def mainloop(self):
        pygame.display.set_caption(FPTV_CAPTION)
        info = pygame.display.Info()
        surface = pygame.display.set_mode((info.current_w, info.current_h), pygame.NOFRAME)
        ui_xid = hex(pygame.display.get_wm_info()['window'])
        print(f"Initial xid: {ui_xid}")

        clock = pygame.time.Clock()

        # Model
        state = State(channels=self.tvh.get_playlist_channels())

        self.mpv.spawn()
        time.sleep(1)
        ui_show(ui_xid)

        # Keyboard support for development (optional)
        dev_keyboard = True

        self.running = True
        while self.running:
            # Pump pygame events (also gives us QUIT)
            for e in pygame.event.get():
                if e.type == pygame.QUIT:
                    self.eventQueue.put(Event.QUIT)
                elif dev_keyboard and e.type == pygame.KEYDOWN:
                    if e.key in (pygame.K_ESCAPE,):
                        self.eventQueue.put(Event.PRESS)  # treat as "back/stop"
                    elif e.key in (pygame.K_q,):
                        self.eventQueue.put(Event.QUIT)
                    elif e.key in (pygame.K_UP, pygame.K_k):
                        self.eventQueue.put(Event.ROT_L)
                    elif e.key in (pygame.K_DOWN, pygame.K_j):
                        self.eventQueue.put(Event.ROT_R)
                    elif e.key in (pygame.K_RETURN, pygame.K_SPACE):
                        self.eventQueue.put(Event.PRESS)

            # Event loop.
            try:
                while True:
                    ev = self.eventQueue.get_nowait()
                    if ev == Event.QUIT:
                        self.running = False
                        break

                    if ev == Event.ROT_R or ev == Event.ROT_L:
                        delta = 1 if ev == Event.ROT_R else -1
                        if state.screen == Screen.MAIN:
                            state.main_index = max(-1, min(2, state.main_index + delta))
                        elif state.screen == Screen.BROWSE and state.channels:
                            state.browse_index = max(-1, min(len(state.channels) - 1, state.browse_index + delta))
                        elif state.screen == Screen.SHUTDOWN:
                            # Only two options: Cancel and Shutdown.
                            state.browse_index = max(-1, min(1, state.browse_index + delta))

                    elif ev == Event.PRESS:
                        if state.screen == Screen.MAIN:
                            if state.main_index == 0:  # Browse
                                state.screen = Screen.BROWSE
                            elif state.main_index == 1:  # Scan (placeholder)
                                state.screen = Screen.SCAN
                            elif state.main_index == 2:  # Shutdown
                                state.screen = Screen.SHUTDOWN
                            else:
                                raise ValueError(f"Unhandled menu index: {state.main_index}")

                        elif state.screen == Screen.SCAN:
                            state.screen = Screen.MAIN

                        elif state.screen == Screen.BROWSE:
                            if not state.channels:
                                continue

                            # Back button.
                            if state.browse_index == -1:
                                state.browse_index = 0
                                state.screen = Screen.MAIN
                                continue

                            ch = state.channels[state.browse_index]
                            state.playing_name = ch.name
                            state.screen = Screen.PLAYING
                            self._set_blanking(True)  # disable blanking while playing
                            ui_hide(ui_xid)
                            self.mpv.play(ch.url)
                            mpv_raise()

                        elif state.screen == Screen.PLAYING:
                            self.mpv.stop()
                            mpv_lower()
                            self._set_blanking(False)
                            ui_show(ui_xid)

                            state.screen = Screen.BROWSE
                            print(f"Player done. Mode is browse")

                        elif state.screen == Screen.SHUTDOWN:
                            if state.browse_index == 1:
                                # Shutdown option.
                                self.eventQueue.put(Event.QUIT)

                            # Back or Cancel
                            else:
                                state.browse_index = 0
                                state.screen = Screen.MAIN

                    else:
                        raise ValueError(f"Unknown event: {ev}")

            except Empty:
                pass

            # Render current screen
            if state.screen == Screen.MAIN:
                draw_menu(surface, self.font_title, self.font_item,
                          ["Browse", "Scan", "Shutdown"], state.main_index)

            elif state.screen == Screen.SCAN:
                draw_menu(surface, self.font_title, self.font_item,
                          ["(not implemented)", "Press to go back"], 1)

            elif state.screen == Screen.SHUTDOWN:
                # Hack
                options = [
                        Channel("Cancel", ""),
                        Channel("Shutdown and Power Off", "")
                        ]
                draw_browse(surface, self.font_item,
                            options, state.browse_index)

            elif state.screen == Screen.BROWSE:
                draw_browse(surface, self.font_item,
                            state.channels, state.browse_index)

            elif state.screen == Screen.PLAYING:
                # We keep drawing a simple "Playing" overlay.
                # mpv will be fullscreen, too.
                # If mpv is fullscreen, you may not see this.
                draw_playing(surface, self.font_title, self.font_item, self.font_small,
                             state.playing_name)
                pass

            pygame.display.flip()
            clock.tick(30)

        self.shutdown()

    def shutdown(self) -> int:
        try:
            print("Shutting down player.")
            self.mpv.shutdown()
            self._set_blanking(False)
            print("Quitting pygame engine.")
            pygame.quit()
        except Exception as e:
            print(f"Error during shutdown: {e}")
            return 1

        print("Bye!")
        return 0
