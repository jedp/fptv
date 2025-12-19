#!/usr/bin/env python3

"""
GPIO access: on Raspberry Pi OS, gpiozero usually works if the user is in
the gpio group (and you’re using the modern gpio character device). If you hit
permission issues, check /dev/gpiochip* permissions and group membership.

mpv output: if you later go console-only (no desktop), you may need to adjust
mpv flags to use KMS/DRM output rather than X11/Wayland. Your existing setup
already works; keep it as-is until you change display stack.

tvheadend auth: if your playlist needs credentials, store them in a config file
readable by toytv (not world-readable) and load at runtime.
"""

# !/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from enum import Enum, auto
from queue import SimpleQueue, Empty
from typing import List, Optional

# --- GPIO is optional for dev ---
USE_GPIO = True
try:
    from gpiozero import Button, RotaryEncoder
except Exception:
    USE_GPIO = False
    Button = None
    RotaryEncoder = None

import pygame


# ---------------------------
# Model
# ---------------------------

@dataclass(frozen=True)
class Channel:
    name: str
    url: str


def get_channels() -> List[Channel]:
    # TODO: Replace with TVHeadend playlist parsing.
    return [
        Channel("PBS (demo)", "https://example.com/pbs.m3u8"),
        Channel("NBC (demo)", "https://example.com/nbc.m3u8"),
        Channel("CBS (demo)", "https://example.com/cbs.m3u8"),
        Channel("Weather (demo)", "https://example.com/weather.m3u8"),
        Channel("Channel 5 (demo)", "https://example.com/5.m3u8"),
        Channel("Channel 6 (demo)", "https://example.com/6.m3u8"),
        Channel("Channel 7 (demo)", "https://example.com/7.m3u8"),
    ]


# ---------------------------
# Events from GPIO
# ---------------------------

class EvType(Enum):
    ROT = auto()  # delta = +1/-1
    PRESS = auto()  # select/back button
    QUIT = auto()  # exit


@dataclass(frozen=True)
class Event:
    t: EvType
    delta: int = 0


# ---------------------------
# Screen blanking helpers (X11)
# ---------------------------

def run_quiet(cmd: List[str]) -> None:
    try:
        subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def x11_blanking_disable() -> None:
    """
    Disable X screen saver + DPMS so the display stays on.
    Requires an X session (your Pi Desktop).
    """
    run_quiet(["xset", "s", "off"])
    run_quiet(["xset", "-dpms"])
    run_quiet(["xset", "s", "noblank"])


def x11_blanking_enable() -> None:
    """
    Re-enable defaults (you can tune these).
    """
    run_quiet(["xset", "s", "on"])
    run_quiet(["xset", "+dpms"])
    run_quiet(["xset", "s", "blank"])


# ---------------------------
# mpv control
# ---------------------------

class MPV:
    def __init__(self) -> None:
        self.p: Optional[subprocess.Popen] = None

    def start(self, url: str) -> None:
        self.stop()

        # For X desktop, mpv will open its own window; we ask it to go fullscreen.
        cmd = [
            "mpv",
            "--fullscreen",
            "--no-terminal",
            "--force-window=yes",  # ensure a window is created (useful for some streams)
            url,
        ]
        self.p = subprocess.Popen(cmd)

    def stop(self) -> None:
        if not self.p:
            return
        if self.p.poll() is None:
            try:
                self.p.terminate()
                self.p.wait(timeout=2)
            except Exception:
                try:
                    self.p.kill()
                except Exception:
                    pass
        self.p = None


# ---------------------------
# UI state machine
# ---------------------------

class Screen(Enum):
    MAIN = auto()
    BROWSE = auto()
    PLAYING = auto()
    SCAN = auto()  # placeholder


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


# ---------------------------
# Rendering helpers
# ---------------------------

def draw_centered_text(surface, font, text, y, bold=False):
    color = (255, 255, 255)
    img = font.render(text, True, color)
    r = img.get_rect(center=(surface.get_width() // 2, y))
    surface.blit(img, r)


def draw_menu(surface, title_font, item_font, title: str, items: List[str], selected: int):
    surface.fill((0, 0, 0))
    draw_centered_text(surface, title_font, title, 90)

    start_y = 200
    line_h = 70
    for i, s in enumerate(items):
        is_sel = (i == selected)
        prefix = "▶ " if is_sel else "  "
        color = (255, 255, 0) if is_sel else (220, 220, 220)
        img = item_font.render(prefix + s, True, color)
        r = img.get_rect(center=(surface.get_width() // 2, start_y + i * line_h))
        surface.blit(img, r)


def draw_browse(surface, title_font, item_font, small_font, channels: List[Channel], selected: int):
    surface.fill((0, 0, 0))
    # Header with Back “affordance”
    header = "◀ Back"
    img = small_font.render(header, True, (180, 180, 180))
    surface.blit(img, (20, 20))

    draw_centered_text(surface, title_font, "Browse", 70)

    if not channels:
        draw_centered_text(surface, item_font, "No channels", surface.get_height() // 2)
        return

    # Show a window around selection
    h = surface.get_height()
    visible = max(5, (h - 200) // 52)
    half = visible // 2
    start = max(0, selected - half)
    end = min(len(channels), start + visible)
    start = max(0, end - visible)

    y0 = 150
    line_h = 52
    for row, idx in enumerate(range(start, end)):
        ch = channels[idx].name
        is_sel = (idx == selected)
        prefix = "▶ " if is_sel else "  "
        color = (255, 255, 0) if is_sel else (220, 220, 220)
        img = item_font.render(prefix + ch, True, color)
        surface.blit(img, (80, y0 + row * line_h))

    # Footer hint
    hint = "Rotate: scroll    Press: select/stop"
    img = small_font.render(hint, True, (160, 160, 160))
    surface.blit(img, (20, surface.get_height() - 40))


def draw_playing(surface, title_font, item_font, small_font, name: str):
    surface.fill((0, 0, 0))
    img = small_font.render("Press to stop and return", True, (160, 160, 160))
    surface.blit(img, (20, 20))
    draw_centered_text(surface, title_font, "Playing", 90)
    draw_centered_text(surface, item_font, name, 190)


# ---------------------------
# GPIO wiring -> event queue
# ---------------------------

def setup_gpio_events(q: SimpleQueue, encoder_a=17, encoder_b=27, button_pin=22):
    if not USE_GPIO:
        return None, None

    enc = RotaryEncoder(encoder_a, encoder_b, max_steps=0)
    btn = Button(button_pin, pull_up=True, bounce_time=0.03)

    last = enc.steps

    def on_rotated():
        nonlocal last
        cur = enc.steps
        d = cur - last
        if d == 0:
            return
        last = cur
        q.put(Event(EvType.ROT, delta=1 if d > 0 else -1))

    def on_pressed():
        q.put(Event(EvType.PRESS))

    enc.when_rotated = on_rotated
    btn.when_pressed = on_pressed
    return enc, btn


# ---------------------------
# Main app loop
# ---------------------------

def main():
    # Make SDL a bit more kiosk-friendly.
    os.environ.setdefault("SDL_VIDEO_CENTERED", "1")

    pygame.init()
    pygame.mouse.set_visible(False)  # hide cursor

    # Fullscreen desktop resolution
    surface = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
    pygame.display.set_caption("Toy TV")

    # Fonts (use default; you can bundle a TTF later)
    title_font = pygame.font.Font(None, 92)
    item_font = pygame.font.Font(None, 56)
    small_font = pygame.font.Font(None, 32)

    clock = pygame.time.Clock()
    q: SimpleQueue[Event] = SimpleQueue()

    # GPIO event source
    enc, btn = setup_gpio_events(q)

    # Model
    state = State(channels=get_channels())
    mpv = MPV()

    # Keyboard support for development (optional)
    dev_keyboard = True

    blanking_is_disabled = False

    def set_blanking(disable: bool):
        nonlocal blanking_is_disabled
        if disable and not blanking_is_disabled:
            x11_blanking_disable()
            blanking_is_disabled = True
        elif (not disable) and blanking_is_disabled:
            x11_blanking_enable()
            blanking_is_disabled = False

    running = True
    while running:
        # Pump pygame events (also gives us QUIT)
        for e in pygame.event.get():
            if e.type == pygame.QUIT:
                q.put(Event(EvType.QUIT))
            elif dev_keyboard and e.type == pygame.KEYDOWN:
                if e.key in (pygame.K_ESCAPE,):
                    q.put(Event(EvType.PRESS))  # treat as “back/stop”
                elif e.key in (pygame.K_q,):
                    q.put(Event(EvType.QUIT))
                elif e.key in (pygame.K_UP, pygame.K_k):
                    q.put(Event(EvType.ROT, delta=-1))
                elif e.key in (pygame.K_DOWN, pygame.K_j):
                    q.put(Event(EvType.ROT, delta=+1))
                elif e.key in (pygame.K_RETURN, pygame.K_SPACE):
                    q.put(Event(EvType.PRESS))

        # Consume queued GPIO/dev events
        try:
            while True:
                ev = q.get_nowait()
                if ev.t == EvType.QUIT:
                    running = False
                    break

                if ev.t == EvType.ROT:
                    if state.screen == Screen.MAIN:
                        state.main_index = max(0, min(2, state.main_index + ev.delta))
                    elif state.screen == Screen.BROWSE and state.channels:
                        state.browse_index = max(0, min(len(state.channels) - 1, state.browse_index + ev.delta))

                elif ev.t == EvType.PRESS:
                    if state.screen == Screen.MAIN:
                        if state.main_index == 0:  # Browse
                            state.screen = Screen.BROWSE
                        elif state.main_index == 1:  # Scan (placeholder)
                            state.screen = Screen.SCAN
                        else:  # Shutdown (for now: exit)
                            running = False

                    elif state.screen == Screen.SCAN:
                        state.screen = Screen.MAIN

                    elif state.screen == Screen.BROWSE:
                        if not state.channels:
                            continue
                        ch = state.channels[state.browse_index]
                        state.playing_name = ch.name
                        state.screen = Screen.PLAYING
                        set_blanking(True)  # disable blanking while playing
                        mpv.start(ch.url)

                    elif state.screen == Screen.PLAYING:
                        mpv.stop()
                        set_blanking(False)
                        state.screen = Screen.BROWSE

        except Empty:
            pass

        # Render current screen
        if state.screen == Screen.MAIN:
            set_blanking(False)
            draw_menu(surface, title_font, item_font, "Toy TV", ["Browse", "Scan", "Shutdown"], state.main_index)

        elif state.screen == Screen.SCAN:
            set_blanking(False)
            draw_menu(surface, title_font, item_font, "Scan", ["(not implemented)", "Press to go back"], 1)

        elif state.screen == Screen.BROWSE:
            set_blanking(False)
            draw_browse(surface, title_font, item_font, small_font, state.channels, state.browse_index)

        elif state.screen == Screen.PLAYING:
            # we keep drawing a simple “Playing” overlay; mpv will be fullscreen too.
            # If mpv is fullscreen, you may not see this. That’s okay.
            draw_playing(surface, title_font, item_font, small_font, state.playing_name)

        pygame.display.flip()
        clock.tick(30)

    # Cleanup
    mpv.stop()
    set_blanking(False)
    pygame.quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
