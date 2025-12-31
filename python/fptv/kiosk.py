#!/usr/bin/env python3
from dataclasses import dataclass
from enum import Enum, auto
from queue import SimpleQueue
from typing import List

import pygame

from fptv.display import Display
from fptv.hw import HwEventBinding
from fptv.input import Action, InputMapper
from fptv.log import Logger
from fptv.tuner import Tuner, TunerState
from fptv.tvh import Channel, TVHeadendScanner, ScanConfig


class Screen(Enum):
    MENU = auto()
    BROWSE = auto()
    TUNE = auto()
    PLAY = auto()
    SCAN = auto()
    SHUTDOWN = auto()
    MAINTENANCE = auto()


VOLUME_INCREMENT = 5
VOLUME_DECREMENT = -5


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
        self._event_queue = SimpleQueue()
        self.tvh = TVHeadendScanner(ScanConfig.from_env())
        self.hw = HwEventBinding(self._event_queue)
        self.input = InputMapper(self._event_queue)
        self.state = State(channels=self.tvh.get_playlist_channels())

        # Display (owns pygame, fonts, overlays, menu renderer)
        self.display = Display()

        # Tuner (owns mpv + watchdog) - must construct after Display for GL context
        self.tuner = Tuner(self.tvh)
        self.display.set_tuner(self.tuner)

    def mainloop(self) -> None:
        self.log.out(f"Loaded {len(self.state.channels)} channels")

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
                    if self.state.screen == Screen.MENU:
                        # Start video mode
                        self.tuner.resume()
                        if self.state.channels:
                            ch = self.state.channels[self.state.browse_index]
                            self.tuner.tune_now(ch.url, f"Channel: {ch.name}")
                            self.display.show_channel_name(f"Channel: {ch.name}", seconds=3.0)
                            self.state.screen = Screen.TUNE
                        else:
                            self.state.screen = Screen.PLAY
                        force_flip = True
                    else:
                        # Return to menu
                        self.state.screen = Screen.MENU
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
                    self.display.show_channel_name(f"Channel: {channel.name}", seconds=3.0)
                    force_flip = True

                    # If in video mode, request debounced tune
                    if self.state.screen in (Screen.PLAY, Screen.TUNE):
                        self.tuner.request_tune(channel.url, f"Channel: {channel.name}")

                elif action == Action.VOLUME_UP:
                    self.tuner.add_volume(VOLUME_INCREMENT)

                elif action == Action.VOLUME_DOWN:
                    self.tuner.add_volume(VOLUME_DECREMENT)

            # --- Render ---
            if self.state.screen in (Screen.PLAY, Screen.TUNE):
                # Render video + overlays
                did_flip, did_render = self.display.render_video(force_flip)
                if did_flip:
                    force_flip = False

                # Tick tuner state machine (after render so we know if frame rendered)
                tune_status = self.tuner.tick(did_render)

                # Update screen based on tuner state
                if tune_status.state == TunerState.PLAYING:
                    self.state.screen = Screen.PLAY
                elif tune_status.state == TunerState.TUNING:
                    self.state.screen = Screen.TUNE
                elif tune_status.state == TunerState.FAILED:
                    self.display.show_channel_name("No signal", seconds=3.0)
                    self.tuner.pause()
                    self.state.screen = Screen.MENU
                    force_flip = True

                # Show status messages (Retrying…, etc.)
                if tune_status.message:
                    seconds = 5.0 if tune_status.message == "Retrying…" else 3.0
                    self.display.show_channel_name(tune_status.message, seconds=seconds)
                    force_flip = True

            else:
                # MENU: draw menu and present
                self.display.render_menu()

        self.shutdown()

    def shutdown(self) -> int:
        try:
            print("Releasing GPIOs.")
            self.hw.close()
            print("Shutting down display (and tuner).")
            self.display.shutdown()
        except Exception as e:
            print(f"Error during shutdown: {e}")
            return -1

        print("Bye!")
        return 0


if __name__ == "__main__":
    raise SystemExit(FPTV().mainloop())
