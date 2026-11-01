#!/usr/bin/env python3
import threading
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
from fptv.tvh import Channel, EPGEvent, MuxStates, TVHeadendScanner, ScanConfig


class Screen(Enum):
    MENU = auto()  # Main menu: Browse, Scan, About
    BROWSE = auto()  # Channel list
    TUNE = auto()  # Tuning in progress
    PLAY = auto()  # Video playing
    SCAN = auto()  # Frequency scan
    ABOUT = auto()  # About screen


# Main menu options
MENU_OPTIONS = ["Browse", "Scan", "About"]

VOLUME_INCREMENT = 5
VOLUME_DECREMENT = -5

# EPG refresh interval (seconds) - fetch "now playing" data periodically on Browse screen
EPG_REFRESH_SECS = 60.0

# How soon to retry if we have no channels yet
CHANNEL_RETRY_SECS = 5.0

SCAN_PROMPT = "Press to start scan"


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

        # Channel scan, which runs in its own thread
        self._scan_thread: threading.Thread | None = None
        self._scan_ok = False
        self._scan_status = SCAN_PROMPT

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

                elif action in (Action.VOLUME_UP, Action.VOLUME_DOWN):
                    delta = VOLUME_INCREMENT if action == Action.VOLUME_UP else VOLUME_DECREMENT
                    self.tuner.add_volume(delta)
                    self.display.show_volume(self.tuner.get_volume())
                    force_flip = True

            # --- Render ---
            if self.state.screen in (Screen.PLAY, Screen.TUNE):
                # Render video + overlays
                did_flip, _ = self.display.render_video(force_flip)
                if did_flip:
                    force_flip = False

                # Tick tuner state machine
                tune_status = self.tuner.tick()

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
                self._maybe_refresh_browse_data()

                self.display.render_browse(
                    self.state.channels,
                    self.state.browse_index,
                    self.state.epg_map,
                )

            elif self.state.screen == Screen.ABOUT:
                self.display.render_about(self._get_about_info(), self.state.about_index)

            elif self.state.screen == Screen.SCAN:
                if self._scan_thread and not self._scan_thread.is_alive():
                    self._finish_scan()
                self.display.render_scan(self._scan_status, self.state.scan_index)

        self.shutdown()

    def _maybe_refresh_browse_data(self) -> None:
        """Start a background refresh of Browse's data if it's due."""
        interval = EPG_REFRESH_SECS if self.state.channels else CHANNEL_RETRY_SECS
        if time.time() - self.state.epg_fetched_at > interval:
            self.state.epg_fetched_at = time.time()
            threading.Thread(target=self._fetch_browse_data, name="fptv-epg", daemon=True).start()

    def _fetch_browse_data(self) -> None:
        """
        Runs in its own thread, because these requests can stall for seconds.
        Channels can be missing if TVHeadend wasn't ready when we started.
        """
        try:
            tvh = TVHeadendScanner(self.tvh.config)
            if not self.state.channels:
                self.state.channels = tvh.get_playlist_channels()
            epg_map = tvh.get_epg_now()
            if epg_map:
                self.state.epg_map = epg_map
        except Exception as e:
            self.log.err(f"Browse refresh failed: {e}")

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
            elif not self._scan_thread:
                self._start_scan()

    def _start_scan(self) -> None:
        """Start a channel scan. It needs the tuners, so stop any stream first."""
        self.tuner.stop()
        self._scan_ok = False
        self._scan_status = "Scanning..."
        self._scan_thread = threading.Thread(target=self._run_scan, name="fptv-scan", daemon=True)
        self._scan_thread.start()

    def _run_scan(self) -> None:
        """Runs in the scan thread. Uses its own scanner, to not share a session."""

        def progress(_msg: str, states: MuxStates) -> None:
            if states.total:
                done = states.total - states.pending - states.active
                self._scan_status = f"Scanning: {done}/{states.total}"

        try:
            self._scan_ok = TVHeadendScanner(self.tvh.config).scan(progress)
        except Exception as e:
            self.log.err(f"Scan failed: {e}")

    def _finish_scan(self) -> None:
        """Pick up the results once the scan thread has finished."""
        self._scan_thread = None
        channels = self.tvh.get_playlist_channels()
        if channels:
            self.state.channels = channels
            self.state.browse_index = 0
            self.state.epg_fetched_at = 0.0

        if self._scan_ok:
            self._scan_status = f"Found {len(channels)} channels"
        else:
            self._scan_status = "Scan failed"

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

        elif screen == Screen.SCAN and not self._scan_thread:
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
