"""
Input handling: translates raw hardware events to semantic actions.
"""
from enum import Enum, auto
from queue import SimpleQueue, Empty
from typing import Iterator

from fptv.event import Event, HwEvent
from fptv.hw import ENCODER_CHANNEL_NAME, ENCODER_VOLUME_NAME


class Action(Enum):
    """Semantic actions for the kiosk state machine."""
    QUIT = auto()
    TOGGLE_MODE = auto()  # Switch between menu and video
    NEXT_CHANNEL = auto()
    PREV_CHANNEL = auto()
    VOLUME_UP = auto()
    VOLUME_DOWN = auto()

    @staticmethod
    def from_event(hw_event: HwEvent) -> "Action | None":
        """Translate a single HwEvent to an Action (or None if not relevant)."""
        ev = hw_event.event
        src = hw_event.source

        if ev == Event.QUIT:
            return Action.QUIT

        if ev == Event.PRESS:
            return Action.TOGGLE_MODE

        if ev == Event.ROT_R:
            if src == ENCODER_CHANNEL_NAME:
                return Action.NEXT_CHANNEL
            elif src == ENCODER_VOLUME_NAME:
                return Action.VOLUME_UP

        if ev == Event.ROT_L:
            if src == ENCODER_CHANNEL_NAME:
                return Action.PREV_CHANNEL
            elif src == ENCODER_VOLUME_NAME:
                return Action.VOLUME_DOWN

        # Ignore other events (RELEASE, LONG_PRESS, etc.)
        return None


class InputMapper:
    """
    Translates raw HwEvents into semantic Actions.

    Usage:
        mapper = InputMapper(hw_event_queue)
        
        # In mainloop:
        for action in mapper.poll():
            if action == Action.TOGGLE_MODE:
                ...
    """

    def __init__(self, event_queue: SimpleQueue):
        self._queue = event_queue

    def poll(self) -> Iterator[Action]:
        """
        Drain the event queue and yield semantic actions.

        Yields:
            Action for each relevant hardware event.
        """
        while True:
            try:
                hw_event: HwEvent = self._queue.get_nowait()
            except Empty:
                break

            action = Action.from_event(hw_event)
            if action is not None:
                yield action
