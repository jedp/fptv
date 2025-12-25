from __future__ import annotations

import time
from queue import SimpleQueue
from typing import Tuple

from gpiozero import RotaryEncoder, Button

from fptv.event import Event

GPIO_ENCODER_A = 17  # GPIO 11
GPIO_ENCODER_B = 27  # GPIO 13
GPIO_ENCODER_BUTTON = 22  # GPIO 15

LONG_PRESS_S = 5.0
_press_t0 = 0


def setup_encoder(q: SimpleQueue) -> Tuple[RotaryEncoder, Button]:
    enc = RotaryEncoder(GPIO_ENCODER_A, GPIO_ENCODER_B, bounce_time=0.002)
    btn = Button(GPIO_ENCODER_BUTTON, pull_up=True, bounce_time=0.05)

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
        global _press_t0
        _press_t0 = time.monotonic()
        q.put(Event.PRESS)

    def on_released():
        global _press_t0
        delta_t = time.monotonic() - _press_t0
        _press_t0 = time.monotonic()

        if delta_t > LONG_PRESS_S:
            q.put(Event.LONG_PRESS)
        else:
            q.put(Event.RELEASE)

    enc.when_rotated = on_rotated
    btn.when_pressed = on_pressed
    btn.when_released = on_released
    print("Encoder GPIOs configured")
    return enc, btn
