#!/usr/bin/env python3
import os
import threading
import time
from dataclasses import dataclass
from enum import Enum, auto
from queue import SimpleQueue, Empty
from typing import List

import pygame

from fptv.event import Event
from fptv.hw import HwEventBinding, ENCODER_CHANNEL_NAME, ENCODER_VOLUME_NAME
from fptv.log import Logger
from fptv.mpv import EmbeddedMPV, MPV_USERAGENT
from fptv.tuner import Tuner, TunerState
from fptv.render import GLMenuRenderer, OverlayManager, init_viewport
from fptv.render import draw_menu_surface, make_text_overlay, make_volume_overlay, clear_screen
from fptv.tvh import Channel, TVHeadendScanner, ScanConfig, WatchdogWorker

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


@dataclass
class TVHStatus:
    t: float
    ok: bool
    subs: dict | None = None
    conns: dict | None = None
    err: str | None = None


class TVHPoller(threading.Thread):
    """
    Poll TVHeadend in a background thread and push latest status into a queue.

    - Never blocks the render thread.
    - Drops old status if the main thread is behind (keeps only the latest).
    """

    def __init__(self, tvh, out_queue: SimpleQueue, interval_s: float = 1.0):
        super().__init__(daemon=True)
        self.tvh = tvh
        self.out_queue = out_queue
        self.interval_s = interval_s
        self._stop_evt = threading.Event()

    def stop(self) -> None:
        self._stop_evt.set()

    def run(self) -> None:
        while not self._stop_evt.is_set():
            t0 = time.time()
            try:
                subs = self.tvh.subscriptions()
                conns = self.tvh.connections()
                status = TVHStatus(t=t0, ok=True, subs=subs, conns=conns)
            except Exception as e:
                status = TVHStatus(t=t0, ok=False, err=str(e))

            # Push status; SimpleQueue can grow, so optionally drain older items:
            try:
                # keep queue small: drop stale statuses
                while True:
                    self.out_queue.get_nowait()
            except Exception:
                pass

            self.out_queue.put(status)

            # sleep remainder
            dt = time.time() - t0
            sleep_for = max(0.05, self.interval_s - dt)
            self._stop_evt.wait(sleep_for)

    def shutdown(self):
        self.stop()
        self.join(timeout=2.0)


class FPTV:
    def __init__(self, screen_w: int = SCREEN_W, screen_h: int = SCREEN_H):
        self.w = screen_w
        self.h = screen_h
        self.log = Logger("fptv")
        self.event_queue = SimpleQueue()
        self.tvh_status_q = SimpleQueue()
        self.tvh = TVHeadendScanner(ScanConfig.from_env())
        self.poller = TVHPoller(self.tvh, self.tvh_status_q, interval_s=1.0)
        self.hw = HwEventBinding(self.event_queue)
        self.state = State(channels=self.tvh.get_playlist_channels())
        self.watch: WatchdogWorker | None = None

        pygame.init()
        pygame.font.init()

        self._init_renderer()  # creates GL context + renderer + overlays
        self.mpv = EmbeddedMPV()
        self.mpv.initialize()  # now GL proc lookup is valid
        self.tuner = Tuner(self.mpv)

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
        self.mpv.pause()

        menu_surf = pygame.Surface((SCREEN_W, SCREEN_H))

        # Channel list
        # self.state.fetch_channels(self.tvh)
        self.log.out(f"Loaded {len(self.state.channels)} channels")

        mode: Screen = Screen.MENU
        self.overlays.set_channel_name("Channel: None")
        force_flip = False

        self.watch = WatchdogWorker(self.tvh, ua_tag=MPV_USERAGENT, interval_s=1.0)
        self.watch.start()

        self.poller.start()

        running = True
        clock = pygame.time.Clock()

        while running:
            clock.tick(60)

            # --- Handle input events ---
            while True:
                try:
                    hwEvent = self.event_queue.get_nowait()
                except Empty:
                    break

                ev_src = hwEvent.source
                ev = hwEvent.event

                if ev == Event.QUIT:
                    running = False

                if ev == Event.PRESS:
                    if mode == Screen.MENU:
                        # Start video mode
                        self.mpv.resume()
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
                        self.mpv.pause()
                        force_flip = True

                if ev == Event.ROT_L or ev == Event.ROT_R:
                    if ev_src == ENCODER_CHANNEL_NAME:
                        if not self.state.channels:
                            continue

                        i = self.state.browse_index + (1 if ev == Event.ROT_R else -1)
                        i = max(0, min(len(self.state.channels) - 1, i))
                        self.state.browse_index = i
                        channel = self.state.channels[self.state.browse_index]
                        self.overlays.set_channel_name(f"Channel: {channel.name}", seconds=3.0)
                        force_flip = True

                        # If in video mode, request debounced tune
                        if mode in (Screen.PLAY, Screen.TUNE):
                            self.tuner.request_tune(channel.url, f"Channel: {channel.name}")

                    elif ev_src == ENCODER_VOLUME_NAME:
                        delta = 5 if ev == Event.ROT_R else -5
                        self.mpv.add_volume(delta)

            # --- Render ---
            init_viewport(self.w, self.h)

            if mode in (Screen.PLAY, Screen.TUNE):
                clear_screen()

                # Render mpv frame
                did_render = self.mpv.maybe_render(self.w, self.h)

                # Tick tuner state machine
                tune_status = self.tuner.tick(did_render)

                # Update mode based on tuner state
                if tune_status.state == TunerState.PLAYING:
                    mode = Screen.PLAY
                elif tune_status.state == TunerState.TUNING:
                    mode = Screen.TUNE
                elif tune_status.state == TunerState.FAILED:
                    self.overlays.set_channel_name("No signal", seconds=3.0)
                    self.mpv.pause()
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

                # --- Watchdog integration ---
                self.watch.expecting = self.tuner.is_expecting_video
                self.watch.current_url = self.tuner.current_url
                self.watch.tuning_started_at = self.tuner.tune_started_at

                # Drain watchdog actions
                while True:
                    try:
                        action, url, reason = self.watch.actions.get_nowait()
                    except Empty:
                        break

                    if action == "reload" and url:
                        self.tuner.reload(reason)
                        mode = Screen.TUNE
                        force_flip = True

                # Present
                if did_render or force_flip:
                    pygame.display.flip()
                    self.mpv.report_swap()
                    force_flip = False

            else:
                # MENU: draw menu and present
                draw_menu_surface(menu_surf, self.font_item, "Press button to toggle video")
                self.renderer.update_from_surface(menu_surf)

                clear_screen()
                self.renderer.draw_fullscreen()
                pygame.display.flip()
                self.mpv.report_swap()

            # Let mpv process pending commands
            self.mpv.tick()

        self.shutdown()

    def shutdown(self) -> int:
        try:
            print("Stopping watchdog worker.")
            self.watch.shutdown()
            print("Stopping TVH poller.")
            self.poller.shutdown()
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
