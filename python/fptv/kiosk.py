#!/usr/bin/env python3

import os
import time
from dataclasses import dataclass
from enum import Enum, auto
from queue import SimpleQueue, Empty
from typing import List, Optional

import pygame

from fptv.event import Event
from fptv.hw import setup_encoder
from fptv.log import Logger
from fptv.mpv import MPV
from fptv.render import draw_menu, draw_browse, draw_escaping
from fptv.tvh import Channel, ScanConfig, TVHeadendScanner

FPTV_CAPTION = "fptv"
ASSETS_FONT = os.path.join(os.path.dirname(os.path.realpath(__file__)), "assets/fonts")

STATUS_EXIT_TO_SHELL = 42


class Screen(Enum):
    MAIN = auto()
    BROWSE = auto()
    PLAYING = auto()
    SCAN = auto()
    SHUTDOWN = auto()
    MAINTENANCE = auto()


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


class App:
    """
    Kiosk app designed for X11-free (console/KMS) operation.
    """

    def __init__(self):
        self.running = False
        self.log = Logger("fptv")
        self.mpv = MPV()
        self.tvh = TVHeadendScanner(ScanConfig.from_env())
        self.eventQueue: SimpleQueue[Event] = SimpleQueue()
        self.blanking_is_disabled = False
        self.gui_visible = False

        self.surface: Optional[pygame.Surface] = None
        self.clock: Optional[pygame.time.Clock] = None

        # Setup hardware GPIOs and rotary encoder.
        # Store references to GPIO objects so they don't get garbage collected.
        self.encoder, self.button = setup_encoder(self.eventQueue)

        os.environ.setdefault("SDL_VIDEO_CENTERED", "1")

        # Bring up the menu UI initially.
        self._use_display_for_menu()

    def _create_display(self) -> None:
        """
        (Re)create the pygame display surface and clock.
        """
        pygame.display.set_caption(FPTV_CAPTION)
        info = pygame.display.Info()
        self.surface = pygame.display.set_mode((info.current_w, info.current_h), pygame.NOFRAME)
        self.clock = pygame.time.Clock()

    def _use_display_for_menu(self) -> None:
        """
        Initialize pygame for menu rendering.

        Use this to re-initialize the display surface after returning from video.
        """
        # Ensure mpv is not holding the DRM device.
        self.mpv.shutdown()

        pygame.init()
        pygame.font.init()
        pygame.mouse.set_visible(False)

        # Fonts
        self.font_title = pygame.font.Font(f"{ASSETS_FONT}/VeraSeBd.ttf", 92)
        self.font_item = pygame.font.Font(f"{ASSETS_FONT}/VeraSe.ttf", 56)
        self.font_small = pygame.font.Font(f"{ASSETS_FONT}/VeraSe.ttf", 32)

        self._create_display()
        self.gui_visible = True

    def _use_display_for_video(self) -> None:
        """
        Release pygame so mpv can take over KMS/DRM fullscreen output.

        This will tear down the pygame video system and release the DRM device.
        So don't call pygame.event.get() etc. after calling this, without calling
        _use_display_for_menu() first.
        """
        self.gui_visible = False
        pygame.quit()
        self.surface = None
        self.clock = None

    def _pump_pygame_events_to_queue(self) -> None:
        """
        Only call this while pygame's video system is initialized (i.e., menu/browse screens).
        """
        if not pygame.display.get_init():
            return

        for e in pygame.event.get():
            if e.type == pygame.QUIT:
                self.eventQueue.put(Event.QUIT)

    def mainloop(self) -> None:
        state = State(channels=self.tvh.get_playlist_channels())

        self.running = True
        while self.running:
            # Only pump pygame events when video is not playing.
            if state.screen != Screen.PLAYING:
                self._pump_pygame_events_to_queue()

            # Consume hardware/UI events from the queue and update state machine.
            try:
                while True:
                    ev = self.eventQueue.get_nowait()

                    if ev == Event.LONG_PRESS:
                        self._use_display_for_menu()
                        state.screen = Screen.MAINTENANCE

                    elif ev == Event.QUIT:
                        self.running = False
                        break

                    # Ignore rotary while playing video; the button is used to exit.
                    elif state.screen == Screen.PLAYING:
                        if ev == Event.PRESS:
                            # Exit video -> return to browsing
                            self._use_display_for_menu()  # re-init pygame + recreate surface
                            state.screen = Screen.BROWSE
                            # Reset browse index if it was on "Back"
                            state.browse_index = max(0, state.browse_index)
                        continue

                    # MENU/BROWSE/SCAN/SHUTDOWN handling
                    elif ev in (Event.ROT_R, Event.ROT_L):
                        delta = 1 if ev == Event.ROT_R else -1

                        if state.screen == Screen.MAIN:
                            state.main_index = max(0, min(2, state.main_index + delta))
                        elif state.screen == Screen.BROWSE and state.channels:
                            # Allow -1 as "Back" item
                            state.browse_index = max(-1, min(len(state.channels) - 1, state.browse_index + delta))
                        elif state.screen == Screen.SHUTDOWN:
                            # Two options: Cancel (0) and Shutdown (1)
                            state.browse_index = max(0, min(1, state.browse_index + delta))

                    elif ev == Event.PRESS:
                        if state.screen == Screen.MAIN:
                            if state.main_index == 0:
                                state.screen = Screen.BROWSE
                            elif state.main_index == 1:
                                state.screen = Screen.SCAN
                            elif state.main_index == 2:
                                state.screen = Screen.SHUTDOWN
                            else:
                                raise ValueError(f"Unhandled menu index: {state.main_index}")

                        elif state.screen == Screen.SCAN:
                            state.screen = Screen.MAIN

                        elif state.screen == Screen.BROWSE:
                            if not state.channels:
                                continue

                            # Back item
                            if state.browse_index == -1:
                                state.browse_index = 0
                                state.screen = Screen.MAIN
                                continue

                            ch = state.channels[state.browse_index]
                            state.playing_name = ch.name
                            state.screen = Screen.PLAYING

                            # Release pygame before handing over to mpv
                            self._use_display_for_video()

                            # mpv takes over fullscreen (DRM/KMS)
                            self.mpv.play(ch.url)

                        elif state.screen == Screen.SHUTDOWN:
                            # 0 = Cancel, 1 = Shutdown
                            if state.browse_index == 1:
                                self.eventQueue.put(Event.QUIT)
                            else:
                                state.browse_index = 0
                                state.screen = Screen.MAIN

                        elif state.screen == Screen.MAINTENANCE:
                            self.log.out(f"Entering maintenance mode: Ignoring event {ev}.")

                        else:
                            raise ValueError(f"Unknown screen: {state.screen}")

                    elif ev == Event.RELEASE:
                        continue

                    else:
                        raise ValueError(f"Unknown event: {ev}")

            except Empty:
                pass

            # 3) Render (only when GUI is visible and pygame is active)
            if self.gui_visible and self.surface is not None and pygame.display.get_init():
                if state.screen == Screen.MAIN:
                    draw_menu(self.surface, self.font_title, self.font_item,
                              ["Browse", "Scan", "Shutdown"], state.main_index)

                elif state.screen == Screen.SCAN:
                    draw_menu(self.surface, self.font_title, self.font_item,
                              ["(not implemented)", "Press to go back"], 1)

                elif state.screen == Screen.SHUTDOWN:
                    options = [
                        Channel("Cancel", ""),
                        Channel("Shutdown and Power Off", ""),
                    ]
                    draw_browse(self.surface, self.font_item, options, state.browse_index)

                elif state.screen == Screen.BROWSE:
                    draw_browse(self.surface, self.font_item, state.channels, state.browse_index)

                elif state.screen == Screen.PLAYING:
                    # In PLAYING, pygame has been quit and we can't draw overlays here.
                    pass

                elif state.screen == Screen.MAINTENANCE:
                    self.log.out("Long press detected. Escaping to shell.")
                    draw_escaping(self.surface, self.font_item, self.font_small)
                    pygame.display.flip()
                    time.sleep(5)
                    try:
                        self.mpv.shutdown()
                    except Exception as e:
                        self.log.err(f"Error shutting down mpv: {e}")
                        pass
                    try:
                        pygame.quit()
                    except Exception as e:
                        self.log.err(f"Error shutting down pygame: {e}")
                        pass
                    raise SystemExit(STATUS_EXIT_TO_SHELL)

                pygame.display.flip()

                if self.clock:
                    self.clock.tick(30)
            else:
                # While playing video (or if display isn't up yet), avoid spinning hot.
                time.sleep(0.02)

        self.shutdown()

    def shutdown(self) -> int:
        try:
            print("Shutting down player.")
            self.mpv.shutdown()
            print("Quitting pygame engine.")
            pygame.quit()
        except Exception as e:
            print(f"Error during shutdown: {e}")
            return 1

        print("Bye!")
        return 0
