#!/usr/bin/env python3

"""
mpv output: if you later go console-only (no desktop), you may need to adjust
mpv flags to use KMS/DRM output rather than X11/Wayland. Your existing setup
already works; keep it as-is until you change display stack.

tvheadend auth: if your playlist needs credentials, store them in a config file
readable by toytv (not world-readable) and load at runtime.
"""

from __future__ import annotations

import json
import os
import pygame
import re
import signal
import socket
import subprocess
import time
import urllib.request
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


GPIO_ENCODER_A = 17 # GPIO 11
GPIO_ENCODER_B = 27 # GPIO 13
GPIO_ENCODER_BUTTON = 22 # GPIO 15

ASSETS_FONT = os.path.join(os.path.dirname(os.path.realpath(__file__)), "assets/fonts")
TITLE_FONT = f"{ASSETS_FONT}/VeraSe.ttf"
ITEM_FONT = f"{ASSETS_FONT}/VeraSe.ttf"
SMALL_FONT = f"{ASSETS_FONT}/VeraSe.ttf"

URL_TVHEADEND = "http://localhost:9981"
URL_PLAYLIST = f"{URL_TVHEADEND}/playlist/channels"

MPV_SOCK="/tmp/fptv-mpv.sock"

UI_WIN = "fptv.fptv"

FG_NORM = (220, 220, 220)
FG_SEL = (0, 0, 0)
BG_NORM = (0, 0, 0)
BG_SEL = (90, 105, 255)
FG_INACT = (180, 180, 180)
FG_ACT = (0, 0, 0)
BG_INACT = (0, 0, 0)
BG_ACT = (90, 105, 255)

# ---------------------------
# Model
# ---------------------------

@dataclass(frozen=True)
class Channel:
    name: str
    url: str


def get_channels() -> List[Channel]:
    """
    tvheadend's /playlist/channels returns an m3u file.
    Lines look like:

        #EXTINF:-1 tvg-id="26e30b9fb6fb20429aac61784fb50ed4" tvg-chno="9.1",KQED-HD
        http://localhost:9981/stream/channelid/520872742?profile=pass
    """

    print(f"opening {URL_PLAYLIST}")
    with urllib.request.urlopen(URL_PLAYLIST, timeout=5) as resp:
        text = resp.read().decode("utf-8", errors="replace")
        print(f"resp:\n\n{text}\n\n")
        lines = text.splitlines()

    channels = []
    name = None

    for line in lines:
        print(f"Processing line {line}")

        if line.startswith('#EXTM3U'):
            continue

        elif line.startswith('#EXTINF'):
            name = line.strip().split(',')[-1].strip()

        elif line.startswith('http://'):
            if name is None:
                raise ValueError(f"No name found before url: {line}")
            channels.append(Channel(name, line))
            name = None

        else:
            raise ValueError(f"Unexpected m3u line: {line}")

    return channels

# ---------------------------
# Events from GPIO
# ---------------------------


class Event(Enum):
    ROT_R = auto()
    ROT_L = auto()
    PRESS = auto()  # select/back button
    QUIT = auto()  # exit


# ---------------------------
# Screen blanking helpers (X11)
# ---------------------------

def run_quiet(cmd: List[str]) -> None:
    try:
        subprocess.run(cmd, check=False,
                       stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
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
    def __init__(self, sock_path: str = MPV_SOCK) -> None:
        self.proc: Optional[subprocess.Popen] = None
        self.sock_path = sock_path

    def spawn(self) -> None:
        if self._is_running():
            return True

        env = os.environ.copy()
        env.setdefault("DISPLAY", ":0")

        # Remove any stale socket.
        try:
            os.unlink(self.sock_path)
        except FileNotFoundError:
            pass


        cmd = [
            "mpv",
            #f"--wid={xid}",
            f"--input-ipc-server={self.sock_path}",
            "--idle=yes",
            "--force-window=yes",

            "--keep-open=yes", # Keep window alive
            "--fullscreen",
            "--ontop=no",
            "--title=mpv-fptv",
            "--no-border",


            "--osc=no",
            "--osd-level=0",
            "--no-terminal",
            "--really-quiet",

            "--image-display-duration=0",
            "--no-input-default-bindings",
            "--background=color", # Make it invisible on startup
            "--background-color=#000000",
            # Optional - may reduce latency / buffering lag
            # "--cache=no",
            # "--untimed=yes",
        ]
        print(f"Exec: {cmd}")

        self.proc = subprocess.Popen(
                cmd,
                env=env,
                # start_new_session=True, # New process group
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE)

        print(f"mpv pid: {self.proc.pid}")
        time.sleep(2.0)
        rc = self.proc.poll()
        print(f"mpv poll: {rc}")
        if rc is not None:
            out, err = self.proc.communicate(timeout=0.2)
            print(f"mpv stdout: {out}")
            print(f"mpv stderr: {err}")
            self.proc = None
            return

        self._wait_for_socket()

    def play(self, url: str) -> None:
        self.spawn()

        # Make player visible again.
        self._cmd(["set_property", "vid", "auto"])

        ok = self._cmd(["loadfile", url, "replace"])
        if not ok:
            print("Error playing. Trying to restart.")
            self.shutdown()
            self.spawn()
            self._cmd(["loadfile", url, "replace"])

        self._cmd(["set_property", "pause", False])


    def stop(self) -> None:
        if not self.proc:
            return

        if self.proc.poll() is not None:
            self.proc = None
            return

        # Stop playback, but keep mpv running.
        self._cmd(["stop"])
        self._cmd(["set_property", "pause", True])
        # Force black screen
        self._cmd(["set_property", "vid", "no"])


    def shutdown(self) -> None:
        if not self.proc:
            return

        if self.proc.poll() is not None:
            self.proc = None
            return

        # Try to shutdown nicely.
        if not self._cmd(["quit"]):
            self.proc.terminate()

        try:
            self.proc.wait(timeout=2)
        except Exception:
            self.proc.kill()
        finally:
            self.proc = None

    def _cmd(self, cmd: list) -> bool:
        """
        Execute a command like ["stop"] or ["loadfile", url, "replace"]
        """

        if not os.path.exists(self.sock_path):
            print(f"Not found: {self.sock_path}")
            return False

        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(0.2)
                s.connect(self.sock_path)
                dumped = json.dumps({"command": cmd}) + "\n"
                print(f"Sending: {dumped.strip()}")
                s.sendall(dumped.encode("utf-8"))
            return True

        except OSError as e:
            print(f"OSError in _cmd: {e}")
            return False

    def _wait_for_socket(self, timeout_s: float = 5.0) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if os.path.exists(self.sock_path):
                return True

            #print("Waiting for socket ...")
            #if self.proc and self.proc.poll() is not None:
            #    err = self.proc.stderr.read().decode("utf-8", "replace") if self.proc.stderr else ""
            #    print(f"mpv exited with error: {err}")
            #    print(err)
            #    return False

            time.sleep(0.02)

        print("Timed out waiting for socket")
        return False

    def _is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


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


def ui_show():
    subprocess.run(["wmctrl", "-x", "-r", UI_WIN, "-b", "remove,below"], check=False)
    subprocess.run(["wmctrl", "-x", "-r", UI_WIN, "-b", "add,above,sticky,skip_taskbar,skip_pager"], check=False)
    subprocess.run(["wmctrl", "-x", "-a", UI_WIN], check=False)


def ui_hide():
    subprocess.run(["wmctrl", "-x", "-r", UI_WIN, "-b", "remove,above"], check=False)
    subprocess.run(["wmctrl", "-x", "-r", UI_WIN, "-b", "add,below"], check=False)


def draw_centered_text(surface, font, text, y, bold=False):
    color = (255, 255, 255)
    img = font.render(text, True, color)
    r = img.get_rect(center=(surface.get_width() // 2, y))
    surface.blit(img, r)


def draw_menu(surface, title_font, item_font, title: str,
              items: List[str], selected: int):
    surface.fill((0, 0, 0))
    draw_centered_text(surface, title_font, title, 90)

    start_y = 200
    line_h = 70
    line_w = surface.get_width()

    for i, text in enumerate(items):
        is_sel = (i == selected)
        # prefix = "▶ " if is_sel else "  "
        prefix = "  "
        bg_color = BG_SEL if is_sel else BG_NORM
        fg_color = FG_SEL if is_sel else FG_NORM

        y = start_y + i * line_h
        rect = pygame.Rect(0, y, line_w, line_h) 
        pygame.draw.rect(surface, bg_color, rect)

        text_surf = item_font.render(prefix + text, True, fg_color)
        text_rect = text_surf.get_rect()
        text_rect.midleft = (20, y + line_h // 2)

        surface.blit(text_surf, text_rect)


def draw_browse(surface, title_font, item_font, small_font,
                channels: List[Channel], selected: int):
    surface.fill(BG_NORM)
    # Header with Back "affordance"
    header = "Back"
    img = small_font.render(header, True, FG_INACT, BG_INACT)
    surface.blit(img, (20, 20))

    draw_centered_text(surface, title_font, "Browse", 70)

    if not channels:
        draw_centered_text(
                surface, item_font, "No channels", surface.get_height() // 2)
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
    line_w = surface.get_width()
    for row, idx in enumerate(range(start, end)):
        text = channels[idx].name
        is_sel = (idx == selected)
        # prefix = "▶ " if is_sel else "  "
        prefix = "  "
        fg_color = FG_SEL if is_sel else FG_NORM
        bg_color = BG_SEL if is_sel else BG_NORM
        y = y0 + row * line_h
        rect = pygame.Rect(0, y, line_w, line_h)
        pygame.draw.rect(surface, bg_color, rect)
        text_surf = item_font.render(text, True, fg_color)
        text_rect = text_surf.get_rect()
        text_rect.midleft = (20, y + line_h // 2)
        surface.blit(text_surf, text_rect)

    # Footer hint
    hint = "Rotate: scroll    Press: select/stop"
    img = small_font.render(hint, True, FG_INACT)
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

def setup_encoder(q: SimpleQueue) -> list:
    if not USE_GPIO:
        return None, None

    enc = RotaryEncoder(GPIO_ENCODER_A, GPIO_ENCODER_B, max_steps=0)
    btn = Button(GPIO_ENCODER_BUTTON, pull_up=True, bounce_time=0.03)

    last = enc.steps

    def on_rotated():
        nonlocal last
        cur = enc.steps
        d = cur - last
        if d == 0:
            return
        last = cur
        q.put(Event.ROT_R if d > 0 else Event.ROT_L)

    def on_pressed():
        q.put(Event.PRESS)

    enc.when_rotated = on_rotated
    btn.when_pressed = on_pressed
    print("Encoder GPIOs configured")
    return enc, btn


# ---------------------------
# Main app loop
# ---------------------------

def main():
    # Make SDL a bit more kiosk-friendly.
    os.environ.setdefault("SDL_VIDEO_CENTERED", "1")
    os.environ["SDL_VIDEO_X11_WMCLASS"] = "fptv"

    pygame.init()
    pygame.mouse.set_visible(False)  # hide cursor

    # Fullscreen desktop resolution
    surface = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
    pygame.display.set_caption("fptv")
    time.sleep(1.0)

    # Fonts (use default; you can bundle a TTF later)
    title_font = pygame.font.Font(TITLE_FONT, 92)
    item_font = pygame.font.Font(ITEM_FONT, 56)
    small_font = pygame.font.Font(SMALL_FONT, 32)

    clock = pygame.time.Clock()
    q: SimpleQueue[Event] = SimpleQueue()

    # GPIO event source
    enc, btn = setup_encoder(q)

    # Model
    state = State(channels=get_channels())

    mpv = MPV()
    mpv.spawn()

    time.sleep(1.0)
    ui_show()

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
                q.put(Event.QUIT)
            elif dev_keyboard and e.type == pygame.KEYDOWN:
                if e.key in (pygame.K_ESCAPE,):
                    q.put(Event.PRESS)  # treat as "back/stop"
                elif e.key in (pygame.K_q,):
                    q.put(Event.QUIT)
                elif e.key in (pygame.K_UP, pygame.K_k):
                    q.put(Event.ROT_L)
                elif e.key in (pygame.K_DOWN, pygame.K_j):
                    q.put(Event.ROT_R)
                elif e.key in (pygame.K_RETURN, pygame.K_SPACE):
                    q.put(Event.PRESS)

        # Consume queued GPIO/dev events
        try:
            while True:
                ev = q.get_nowait()
                if ev == Event.QUIT:
                    running = False
                    break

                if ev == Event.ROT_R or ev == Event.ROT_L:
                    delta = 1 if ev==Event.ROT_R else -1
                    if state.screen == Screen.MAIN:
                        state.main_index = max(0, min(2, state.main_index + delta))
                    elif state.screen == Screen.BROWSE and state.channels:
                        state.browse_index = max(0, min(len(state.channels) - 1, state.browse_index + delta))

                elif ev == Event.PRESS:
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
                        ui_hide()
                        mpv.play(ch.url)

                    elif state.screen == Screen.PLAYING:
                        mpv.stop()
                        set_blanking(False)
                        ui_show()
                        state.screen = Screen.BROWSE
                        print(f"Player done. Mode is browse")

                else:
                    raise ValueError(f"Unknown event: {ev}")

        except Empty:
            pass

        # Render current screen
        if state.screen == Screen.MAIN:
            draw_menu(surface, title_font, item_font,
                      "FPTV", ["Browse", "Scan", "Shutdown"], state.main_index)

        elif state.screen == Screen.SCAN:
            draw_menu(surface, title_font, item_font,
                      "Scan", ["(not implemented)", "Press to go back"], 1)

        elif state.screen == Screen.BROWSE:
            draw_browse(surface, title_font, item_font, small_font,
                        state.channels, state.browse_index)

        elif state.screen == Screen.PLAYING:
            # We keep drawing a simple "Playing" overlay.
            # mpv will be fullscreen, too.
            # If mpv is fullscreen, you may not see this.
            draw_playing(surface, title_font, item_font, small_font,
                         state.playing_name)
            pass

        pygame.display.flip()
        clock.tick(30)

    # Cleanup
    mpv.shutdown()
    set_blanking(False)
    pygame.quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
