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
from fptv.mpv import EmbeddedMPV, MPV_USERAGENT
from fptv.render import GLMenuRenderer, OverlayManager, init_viewport
from fptv.render import draw_menu_surface, make_text_overlay, make_volume_overlay, clear_screen
from fptv.tvh import Channel, TVHeadendScanner, ScanConfig, TVHWatchdog

MPV_FORMAT_FLAG = 3

FPTV_CAPTION = "fptv"
ASSETS_FONT = os.path.join(os.path.dirname(os.path.realpath(__file__)), "assets/fonts")

SCREEN_H = 480
SCREEN_W = 800

DEBOUNCE_S = 0.150  # a feel-good number
MIN_TUNE_INTERVAL_S = 0.35  # prevents hammering tvheadend/mpv
WATCHDOG_EVERY_S = 3.0
TUNE_TIMEOUT_S = 20.0
MAX_TUNE_RETRIES = 2


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
        self.event_queue = SimpleQueue()
        self.tvh = TVHeadendScanner(ScanConfig.from_env())
        self.hw = FPTVHW(self.event_queue)
        self.state = State(channels=self.tvh.get_playlist_channels())

        pygame.init()
        pygame.font.init()

        self._init_renderer()  # creates GL context + renderer + overlays
        self.mpv = EmbeddedMPV()
        self.mpv.initialize()  # now GL proc lookup is valid

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

        # Startup: pause on test source; user presses to enter video mode.
        self.mpv.initialize()
        self.mpv.loadfile("av://lavfi:mandelbrot")
        self.mpv.set_property_flag("pause", True)

        menu_surf = pygame.Surface((SCREEN_W, SCREEN_H))

        # Channel list
        # self.state.fetch_channels(self.tvh)
        self.log.out(f"Loaded {len(self.state.channels)} channels")

        mode: Screen = Screen.MENU
        self.overlays.set_channel_name("Channel: None")

        pending_url: str | None = None
        pending_name: str | None = None
        debounce_deadline = 0.0
        force_flip = False

        active_url: str | None = None

        tuning_started_at = 0.0
        tune_attempts = 0

        watchdog = TVHWatchdog(self.tvh, ua_tag=MPV_USERAGENT)
        next_watchdog_at = 0.0

        running = True
        last_frame_at = time.time()
        clock = pygame.time.Clock()

        while running:
            clock.tick(60)

            try:
                ev = self.event_queue.get_nowait()
                if ev == Event.QUIT:
                    running = False

                if ev == Event.PRESS:
                    if mode == Screen.MENU:
                        # Start video mode. If we don't have an active channel yet, tune to current selection.
                        self.mpv.set_property_flag("pause", False)
                        if self.state.channels:
                            ch = self.state.channels[self.state.browse_index]
                            pending_name = f"Channel: {ch.name}"
                            pending_url = ch.url
                            debounce_deadline = time.time()  # tune immediately
                            self.overlays.set_channel_name(pending_name)
                            mode = Screen.TUNE
                            tuning_started_at = time.time()
                            active_url = ch.url
                        else:
                            mode = Screen.PLAY
                        force_flip = True
                    else:
                        mode = Screen.MENU
                        self.mpv.set_property_flag("pause", True)
                        force_flip = True

                if ev == Event.ROT_L or ev == Event.ROT_R:
                    if not self.state.channels:
                        continue

                    i = self.state.browse_index + (1 if ev == Event.ROT_R else -1)
                    i = max(0, min(len(self.state.channels) - 1, i))
                    self.state.browse_index = i
                    channel = self.state.channels[self.state.browse_index]
                    self.overlays.set_channel_name(f"Channel: {channel.name}")
                    force_flip = True

                    # If we're in video mode, schedule a debounced tune to the newly selected channel.
                    if mode in (Screen.PLAY, Screen.TUNE):
                        pending_url = channel.url
                        pending_name = f"Channel: {channel.name}"
                        debounce_deadline = time.time() + DEBOUNCE_S

            except Empty:
                pass

            # Render
            init_viewport(self.w, self.h)
            now = time.time()

            if mode in (Screen.PLAY, Screen.TUNE):
                clear_screen()

                # Render mpv if it has a new frame ready.
                did_render = self.mpv.maybe_render(self.w, self.h)
                if did_render:
                    last_frame_at = now

                # Overlays (channel name / volume / messages)
                self.overlays.tick()
                self.overlays.draw()

                # Fire a debounced tune request.
                if pending_url and now >= debounce_deadline:
                    active_url = pending_url
                    active_name = pending_name or ""
                    pending_url = None

                    self.log.out(f"Tune to channel: {active_name}")
                    self.mpv.loadfile_now(active_url)
                    tuning_started_at = now
                    tune_attempts = 0
                    mode = Screen.TUNE

                # If we got *any* new frame while tuning, consider the tune successful.
                if mode == Screen.TUNE and did_render and (now - tuning_started_at) > 0.2:
                    mode = Screen.PLAY

                # Tune timeout / retries (handles 'No input detected' cases)
                if mode == Screen.TUNE and (now - tuning_started_at) > TUNE_TIMEOUT_S:
                    if tune_attempts < MAX_TUNE_RETRIES:
                        tune_attempts += 1
                        tuning_started_at = now
                        self.log.out(f"Tune timeout; retry {tune_attempts}/{MAX_TUNE_RETRIES}")
                        self.overlays.set_channel_name("Retrying…")
                        if active_url:
                            self.mpv.loadfile_now(active_url)
                    else:
                        self.log.out("Tune failed; returning to menu")
                        self.overlays.set_channel_name("No signal")
                        self.mpv.set_property_flag("pause", True)
                        mode = Screen.MENU
                        force_flip = True

                # --- watchdog (1 Hz) ---
                if active_url and now >= next_watchdog_at:
                    # Only check TVH while tuning, or if we haven't rendered a new frame recently.
                    should_check = (mode == Screen.TUNE) or ((now - last_frame_at) > 1.0)

                    if should_check:
                        next_watchdog_at = now + WATCHDOG_EVERY_S  # or 2.0 / 5.0
                        expecting = (mode in (Screen.TUNE, Screen.PLAY))
                        if expecting and mode == Screen.TUNE and (now - tuning_started_at) < 1.0:
                            expecting = False

                        t0 = time.time()
                        if watchdog.check_and_fix(now=now, mpv=self.mpv, current_url=active_url, expecting=expecting):
                            self.overlays.set_channel_name("Recovering stream…")
                            force_flip = True
                        dt = time.time() - t0
                        if dt > 0.05:
                            self.log.out(f"watchdog took {dt:.3f}s")
                    else:
                        # Defer without doing any work (keeps watchdog from hammering TVH).
                        next_watchdog_at = now + 0.25

                # Present a new frame if mpv rendered, or if we need to show overlay/menu changes.
                if did_render or force_flip:
                    pygame.display.flip()
                    self.mpv.report_swap()
                    force_flip = False

            else:
                # MENU: draw menu into texture and present
                draw_menu_surface(menu_surf, self.font_item, "Press button to toggle video")
                self.renderer.update_from_surface(menu_surf)

                clear_screen()
                self.renderer.draw_fullscreen()
                pygame.display.flip()
                self.mpv.report_swap()

            # Let mpv advance any pending tune (non-blocking)
            self.mpv.tick()

        self.shutdown()

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
