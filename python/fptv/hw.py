#!/usr/bin/env python3

import time
from dataclasses import dataclass
from queue import SimpleQueue, Empty
from typing import Tuple, Callable

from gpiozero import RotaryEncoder, Button, Device

from fptv.event import Event, HwEvent

GPIO_ENC_CHANNEL_A = 17  # pin 11
GPIO_ENC_CHANNEL_B = 27  # pin 13
GPIO_ENC_CHANNEL_BUTTON = 22  # pin 15

GPIO_ENC_VOLUME_A = 5  # pin 29
GPIO_ENC_VOLUME_B = 6  # pin 31
GPIO_ENC_SHUTDOWN_BUTTON = None  # gpio23=pin16 - configured in /boot/firmware/config.txt

ENCODER_CHANNEL_NAME = "channel"
ENCODER_VOLUME_NAME = "volume"


class EmptyButton:
    def close(self):
        pass

    when_pressed: Callable | None = None
    when_released: Callable | None = None


@dataclass
class RotaryEncoderGPIOs:
    gpio_rot_a: int
    gpio_rot_b: int
    gpio_button: int | None

    def __str__(self):
        return f"RotaryEncoderGPIOs(a={self.gpio_rot_a}, b={self.gpio_rot_b}, button={self.gpio_button})"


volumeEncoderGPIOs = RotaryEncoderGPIOs(GPIO_ENC_VOLUME_A, GPIO_ENC_VOLUME_B, GPIO_ENC_SHUTDOWN_BUTTON)
channelEncoderGPIOs = RotaryEncoderGPIOs(GPIO_ENC_CHANNEL_A, GPIO_ENC_CHANNEL_B, GPIO_ENC_CHANNEL_BUTTON)

LONG_PRESS_S = 5.0
_press_t0 = 0


def _setup_encoder(name: str, gpios: RotaryEncoderGPIOs, q: SimpleQueue) -> Tuple[RotaryEncoder, Button]:
    enc = RotaryEncoder(gpios.gpio_rot_a, gpios.gpio_rot_b, bounce_time=0.002)
    if gpios.gpio_button is None:
        btn = EmptyButton()
    else:
        btn = Button(gpios.gpio_button, pull_up=True, bounce_time=0.05)

    last = enc.steps

    def on_rotated():
        nonlocal last
        cur = enc.steps
        d = cur - last
        if d == 0:
            return
        last = cur
        if d > 0:
            q.put(HwEvent(name, Event.ROT_R))
        else:
            q.put(HwEvent(name, Event.ROT_L))

    def on_pressed():
        global _press_t0
        _press_t0 = time.monotonic()
        q.put(HwEvent(name, Event.PRESS))

    def on_released():
        global _press_t0
        delta_t = time.monotonic() - _press_t0
        _press_t0 = time.monotonic()

        if delta_t > LONG_PRESS_S:
            q.put(HwEvent(name, Event.LONG_PRESS))
        else:
            q.put(HwEvent(name, Event.RELEASE))

    enc.when_rotated = on_rotated
    btn.when_pressed = on_pressed
    btn.when_released = on_released
    print(f"Encoder configured: '{name}' {gpios}")
    return enc, btn


class HwEventBinding:
    def __init__(self, q: SimpleQueue):
        self.q = q

        # Channel selection (end); Mode selection (btn).
        self.vol_enc, self.vol_btn = _setup_encoder(ENCODER_VOLUME_NAME, volumeEncoderGPIOs, self.q)
        self.chan_enc, self.chan_btn = _setup_encoder(ENCODER_CHANNEL_NAME, channelEncoderGPIOs, self.q)

    def close(self):
        self.vol_enc.close()
        self.vol_btn.close()
        self.chan_enc.close()
        self.chan_btn.close()
        # For good measure.
        Device.pin_factory.close()
        print("GPIOs cleaned up")


if __name__ == '__main__':
    q = SimpleQueue()
    hw = HwEventBinding(q)

    try:
        ev = q.get_nowait()
        print(ev)
    except Empty:
        pass

    finally:
        hw.close()
