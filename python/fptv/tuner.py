"""
Tuner: encapsulates debouncing, tune state machine, timeout/retry logic.
"""
import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional, TYPE_CHECKING

from fptv.log import Logger

if TYPE_CHECKING:
    from fptv.mpv import EmbeddedMPV


class TunerState(Enum):
    IDLE = auto()  # Not tuning, no video expected
    TUNING = auto()  # Tune in progress, waiting for frames
    PLAYING = auto()  # Successfully receiving frames
    FAILED = auto()  # Tune failed after retries


@dataclass
class TunerStatus:
    state: TunerState
    channel_name: str
    message: Optional[str] = None  # "Retrying…", "No signal", etc.


class Tuner:
    """
    High-level tuning controller that wraps EmbeddedMPV.
    
    Handles:
    - Input debouncing (coalesces rapid channel changes)
    - Tune timeout and retry logic
    - State machine (IDLE → TUNING → PLAYING or FAILED)
    
    Usage:
        tuner = Tuner(mpv)
        tuner.request_tune(url, "Channel: PBS")  # debounced
        
        # In render loop:
        status = tuner.tick(did_render_frame)
        if status.message:
            overlay.set_text(status.message)
    """

    def __init__(
            self,
            mpv: "EmbeddedMPV",
            debounce_s: float = 0.150,
            tune_timeout_s: float = 20.0,
            max_retries: int = 2,
            frame_grace_s: float = 0.2,  # ignore early frames (buffered)
    ):
        self.mpv = mpv
        self.log = Logger("tuner")

        # Timing config
        self._debounce_s = debounce_s
        self._tune_timeout_s = tune_timeout_s
        self._max_retries = max_retries
        self._frame_grace_s = frame_grace_s

        # State
        self._state = TunerState.IDLE
        self._current_url: Optional[str] = None
        self._current_name: str = ""

        # Pending request (debounced)
        self._pending_url: str | None = None
        self._pending_name: str = ""
        self._debounce_deadline: float = 0.0

        # Tune tracking
        self._tune_started_at: float = 0.0
        self._tune_attempts: int = 0
        self._status_message: str | None = None

    @property
    def state(self) -> TunerState:
        return self._state

    @property
    def current_url(self) -> str | None:
        return self._current_url

    @property
    def current_name(self) -> str:
        return self._current_name or self._pending_name

    @property
    def tune_started_at(self) -> float:
        return self._tune_started_at

    @property
    def is_expecting_video(self) -> bool:
        """True if we're tuning or playing (for watchdog)."""
        return self._state in (TunerState.TUNING, TunerState.PLAYING)

    def request_tune(self, url: str, name: str = "") -> None:
        """
        Request a channel tune with debouncing.
        
        Rapid calls will coalesce - only the last one fires after debounce window.
        """
        self._pending_url = url
        self._pending_name = name
        self._debounce_deadline = time.time() + self._debounce_s
        self._status_message = None

    def tune_now(self, url: str, name: str = "") -> None:
        """
        Tune immediately without debouncing.
        
        Use for initial tune or watchdog recovery.
        """
        self._pending_url = url
        self._pending_name = name
        self._debounce_deadline = 0.0  # fire immediately
        self._status_message = None

    def cancel(self) -> None:
        """Cancel any pending tune and go idle."""
        self._pending_url = None
        self._pending_name = ""
        self._state = TunerState.IDLE
        self._status_message = None

    def tick(self, did_render_frame: bool) -> TunerStatus:
        """
        Call every frame. Handles state transitions and returns current status.
        
        Args:
            did_render_frame: True if mpv rendered a new frame this tick
            
        Returns:
            TuneStatus with current state and any message for overlay display
        """
        now = time.time()
        self._status_message = None

        # --- Check for pending debounced tune ---
        if self._pending_url and now >= self._debounce_deadline:
            self._fire_tune(self._pending_url, self._pending_name)
            self._pending_url = None
            self._pending_name = ""

        # --- State machine transitions ---
        if self._state == TunerState.TUNING:
            time_since_tune = now - self._tune_started_at

            # Success: got a frame after grace period
            if did_render_frame and time_since_tune > self._frame_grace_s:
                self._state = TunerState.PLAYING
                self.log.out(f"Tune success: {self._current_name}")

            # Timeout: retry or fail
            elif time_since_tune > self._tune_timeout_s:
                if self._tune_attempts < self._max_retries:
                    self._tune_attempts += 1
                    self._tune_started_at = now
                    self._status_message = "Retrying…"
                    self.log.out(f"Tune timeout; retry {self._tune_attempts}/{self._max_retries}")
                    if self._current_url:
                        self.mpv.loadfile_now(self._current_url)
                else:
                    self._state = TunerState.FAILED
                    self._status_message = "No signal"
                    self.log.out("Tune failed; max retries exceeded")

        return TunerStatus(
            state=self._state,
            channel_name=self._current_name,
            message=self._status_message,
        )

    def reload(self, reason: str = "") -> None:
        """
        Reload current URL (for watchdog recovery).
        """
        if not self._current_url:
            return

        self.log.out(f"Reload: {reason}")
        self.mpv.stop()
        self.mpv.loadfile_now(self._current_url)
        self._tune_started_at = time.time()
        self._tune_attempts = 0
        self._state = TunerState.TUNING

    def _fire_tune(self, url: str, name: str) -> None:
        """Actually start the tune."""
        self._current_url = url
        self._current_name = name
        self._tune_started_at = time.time()
        self._tune_attempts = 0
        self._state = TunerState.TUNING

        self.log.out(f"Tune to: {name}")
        self.mpv.loadfile_now(url)
