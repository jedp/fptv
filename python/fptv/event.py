from dataclasses import dataclass
from enum import Enum, auto


class Event(Enum):
    ROT_R = auto()
    ROT_L = auto()
    PRESS = auto()  # select/back button
    LONG_PRESS = auto()
    RELEASE = auto()
    QUIT = auto()  # exit


@dataclass
class HwEvent:
    source: str
    event: Event

    def __str__(self):
        return f"HwEvent(source={self.source}, event={self.event})"

    def __repr__(self):
        return self.__str__()
