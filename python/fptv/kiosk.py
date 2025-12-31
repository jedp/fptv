#!/usr/bin/env python3
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from queue import SimpleQueue

import pygame

from fptv.display import Display
from fptv.hw import HwEventBinding
from fptv.input import Action, InputMapper
from fptv.log import Logger
from fptv.tuner import Tuner, TunerState
from fptv.tvh import Channel, EPGEvent, TVHeadendScanner, ScanConfig


class Screen(Enum):
    MENU = auto()  # Main menu: Browse, Scan, About
    BROWSE = auto()  # Channel list
    TUNE = auto()  # Tuning in progress
    PLAY = auto()  # Video playing
    SCAN = auto()  # Frequency scan (future)
    ABOUT = auto()  # About screen


# Main menu options
MENU_OPTIONS = ["Browse", "Scan", "About"]

VOLUME_INCREMENT = 5
VOLUME_DECREMENT = -5

# EPG refresh interval (seconds) - fetch "now playing" data periodically on Browse screen
EPG_REFRESH_SECS = 60.0


@dataclass
class State:
    screen: Screen = Screen.MENU
    menu_index: int = 0  # Main menu selection (0=Browse, 1=Scan, 2=About)
    browse_index: int = 0  # Channel list selection (-1 = Back)
    about_index: int = 0  # About screen (-1 = Back, 0 = content)
    scan_index: int = 0  # Scan screen (-1 = Back, 0 = content)
    channels: list[Channel] | None = None

    # EPG (now playing) data
    epg_map: dict[str, EPGEvent] = field(default_factory=dict)
    epg_fetched_at: float = 0.0

    def __post_init__(self):
        if self.channels is None:
            self.channels = []

    @property
    def current_channel(self) -> Channel | None:
        """Get currently selected channel, or None if no channels."""
        if self.channels and 0 <= self.browse_index < len(self.channels):
            return self.channels[self.browse_index]
        return None


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
                    self._handle_button_press()
                    force_flip = True

                elif action in (Action.NEXT_CHANNEL, Action.PREV_CHANNEL):
                    delta = 1 if action == Action.NEXT_CHANNEL else -1
                    self._handle_wheel(delta)
                    force_flip = True

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

                # Tick tuner state machine
                tune_status = self.tuner.tick(did_render)

                # Update screen based on tuner state
                if tune_status.state == TunerState.PLAYING:
                    self.state.screen = Screen.PLAY
                elif tune_status.state == TunerState.TUNING:
                    self.state.screen = Screen.TUNE
                elif tune_status.state == TunerState.FAILED:
                    self.display.show_channel_name("No signal", seconds=3.0)
                    self.tuner.pause()
                    self.state.screen = Screen.BROWSE
                    force_flip = True

                # Show status messages (Retrying…, etc.)
                if tune_status.message:
                    seconds = 5.0 if tune_status.message == "Retrying…" else 3.0
                    self.display.show_channel_name(tune_status.message, seconds=seconds)
                    force_flip = True

            elif self.state.screen == Screen.MENU:
                self.display.render_main_menu(MENU_OPTIONS, self.state.menu_index)

            elif self.state.screen == Screen.BROWSE:
                # Refresh EPG data periodically
                if time.time() - self.state.epg_fetched_at > EPG_REFRESH_SECS:
                    self.state.epg_map = self.tvh.get_epg_now()
                    self.state.epg_fetched_at = time.time()

                self.display.render_browse(
                    self.state.channels,
                    self.state.browse_index,
                    self.state.epg_map,
                )

            elif self.state.screen == Screen.ABOUT:
                self.display.render_about(self._get_about_info(), self.state.about_index)

            elif self.state.screen == Screen.SCAN:
                self.display.render_scan("Not implemented yet", self.state.scan_index)

        self.shutdown()

    def _handle_button_press(self) -> None:
        """Handle button press based on current screen."""
        screen = self.state.screen

        if screen == Screen.MENU:
            # Select menu option
            option = MENU_OPTIONS[self.state.menu_index]
            if option == "Browse":
                self.state.screen = Screen.BROWSE
            elif option == "Scan":
                self.state.screen = Screen.SCAN
            elif option == "About":
                self.state.screen = Screen.ABOUT

        elif screen == Screen.BROWSE:
            if self.state.browse_index == -1:
                # Back button selected - return to main menu
                self.state.screen = Screen.MENU
                self.state.browse_index = 0  # Reset for next time
            else:
                # Play selected channel
                ch = self.state.current_channel
                if ch:
                    self.tuner.resume()
                    self.tuner.tune_now(ch.url, ch.name)
                    self.display.show_channel_name(ch.name, seconds=3.0)
                    self.state.screen = Screen.TUNE

        elif screen in (Screen.PLAY, Screen.TUNE):
            # Return to browse (at current channel position)
            self.tuner.cancel()
            self.tuner.pause()
            self.state.screen = Screen.BROWSE

        elif screen == Screen.ABOUT:
            if self.state.about_index == -1:
                # Back button selected
                self.state.screen = Screen.MENU
                self.state.about_index = 0  # Reset for next time

        elif screen == Screen.SCAN:
            if self.state.scan_index == -1:
                # Back button selected
                self.state.screen = Screen.MENU
                self.state.scan_index = 0  # Reset for next time

    def _handle_wheel(self, delta: int) -> None:
        """Handle wheel rotation based on current screen."""
        screen = self.state.screen

        if screen == Screen.MENU:
            # Navigate menu
            i = self.state.menu_index + delta
            self.state.menu_index = max(0, min(len(MENU_OPTIONS) - 1, i))

        elif screen == Screen.BROWSE:
            # Navigate channel list (-1 = Back button)
            if self.state.channels:
                i = self.state.browse_index + delta
                self.state.browse_index = max(-1, min(len(self.state.channels) - 1, i))
            else:
                # No channels - only Back button is available
                self.state.browse_index = -1

        elif screen in (Screen.PLAY, Screen.TUNE):
            # Change channel (debounced)
            if self.state.channels:
                i = self.state.browse_index + delta
                self.state.browse_index = max(0, min(len(self.state.channels) - 1, i))
                ch = self.state.current_channel
                if ch:
                    self.display.show_channel_name(ch.name, seconds=3.0)
                    self.tuner.request_tune(ch.url, ch.name)

        elif screen == Screen.ABOUT:
            # Scroll up to Back (-1), down to content (0)
            i = self.state.about_index + delta
            self.state.about_index = max(-1, min(0, i))

        elif screen == Screen.SCAN:
            # Scroll up to Back (-1), down to content (0)
            i = self.state.scan_index + delta
            self.state.scan_index = max(-1, min(0, i))

    def _get_about_info(self) -> dict[str, str]:
        """Get device info for about screen (placeholder)."""
        return {
            "Version": "1.0.0",
            "Device": "Fisher Price TV",
            "IP": "Loading...",  # TODO: get actual IP
        }

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
