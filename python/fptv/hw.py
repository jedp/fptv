from __future__ import annotations

from queue import SimpleQueue
from typing import Tuple

from gpiozero import RotaryEncoder, Button

from fptv.event import Event

GPIO_ENCODER_A = 17  # GPIO 11
GPIO_ENCODER_B = 27  # GPIO 13
GPIO_ENCODER_BUTTON = 22  # GPIO 15


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
        q.put(Event.PRESS)

    enc.when_rotated = on_rotated
    btn.when_pressed = on_pressed
    print("Encoder GPIOs configured")
    return enc, btn
