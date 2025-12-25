from __future__ import annotations

from enum import Enum, auto


class Event(Enum):
    ROT_R = auto()
    ROT_L = auto()
    PRESS = auto()  # select/back button
    LONG_PRESS = auto()
    RELEASE = auto()
    QUIT = auto()  # exit
