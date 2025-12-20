#!/usr/bin/env python3

"""
Rotary encoder test.

Encoder has 220Ω between Pi and A, B, and Button pins.
A, B, and Button pins have 0.01mF to GND.
"""

import os
import pygame
import subprocess
import time
from dataclasses import dataclass
from enum import Enum, auto
from gpiozero import Button, RotaryEncoder
from queue import SimpleQueue, Empty
from typing import List, Optional


GPIO_ENCODER_A = 17 # Pin 11
GPIO_ENCODER_B = 27 # Pin 13
GPIO_ENCODER_BUTTON = 22 # Pin 15


class EvType(Enum):
    ROT = auto()  # delta = +1/-1
    PRESS = auto()  # select/back button
    QUIT = auto()  # exit


@dataclass(frozen=True)
class Event:
    t: EvType
    delta: int = 0


def follow_inputs():
    from gpiozero import DigitalInputDevice
    import time

    a = DigitalInputDevice(GPIO_ENCODER_A)
    b = DigitalInputDevice(GPIO_ENCODER_B)

    old_a = a.value
    old_b = b.value

    print(f"Starting values: a={old_a}, b={old_b}")

    while True:
        new_a = a.value
        new_b = b.value

        if old_a != new_a or old_b != new_b:
            print(f"a={old_a}, b={old_b}")
            old_a = new_a
            old_b = new_b

        time.sleep(0.05)


def encoder_setup(q: SimpleQueue):
    encoder = RotaryEncoder(GPIO_ENCODER_A, GPIO_ENCODER_B, bounce_time=0.002)
    button = Button(GPIO_ENCODER_BUTTON, pull_up=True, bounce_time=0.05)

    last = encoder.steps

    def on_rotated():
        nonlocal last
        cur = encoder.steps
        d = cur - last
        if d == 0:
            return

        last = cur
        q.put(Event(EvType.ROT, delta=1 if d > 0 else -1))

    def on_pressed():
        q.put(Event(EvType.PRESS))

    encoder.when_rotated = on_rotated
    button.when_pressed = on_pressed
    return encoder, button


def main():
    import sys
    q: SimpleQueue[Event] = SimpleQueue()

    encoder, button = encoder_setup(q)

    print("Polling ...")
    while True:
        try:
            e = q.get_nowait()

            print(f"{e.t}")

            if e.t == EvType.QUIT:
                sys.exit(0)

        except Empty:
            time.sleep(0.001)

        except Exception as e:
            print(f"oops {e}")
            
            raise

    
if __name__ == '__main__':
    follow_inputs()
    # main()


