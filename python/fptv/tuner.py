"""
Tuner: high-level video tuning interface that owns and hides EmbeddedMPV.

Encapsulates:
- mpv lifecycle (init, render, shutdown)
- Input debouncing (coalesces rapid channel changes)
- Tune timeout and retry logic
- State machine (IDLE → TUNING → PLAYING or FAILED)
"""
import time
from dataclasses import dataclass
from enum import Enum, auto

from fptv.log import Logger
from fptv.mpv import EmbeddedMPV, MPV_USERAGENT


class TunerState(Enum):
    """State machine for channel tuning."""
    IDLE = auto()  # Not tuning, no video expected
    TUNING = auto()  # Tune in progress, waiting for frames
    PLAYING = auto()  # Successfully receiving frames
    FAILED = auto()  # Tune failed after retries


@dataclass
class TunerStatus:
    """Snapshot of tuner state for UI display."""
    state: TunerState
    channel_name: str
    message: str | None = None  # "Retrying…", "No signal", etc.
    did_render: bool = False  # True if a frame was rendered this tick


class Tuner:
    """
    High-level video tuning controller that owns the embedded player.
    
    The embedded mpv player is an implementation detail.
    
    Handles:
    - mpv lifecycle (initialize, render, shutdown)
    - Input debouncing (coalesces rapid channel changes)
    - Tune timeout and retry logic
    - State machine (IDLE → TUNING → PLAYING or FAILED)
    
    Usage:
        tuner = Tuner()
        tuner.initialize()
        
        # Enter video mode
        tuner.resume()
        tuner.tune_now(url, "Channel: PBS")
        
        # In render loop:
        status = tuner.render_tick(width, height)
        if status.did_render:
            pygame.display.flip()
            tuner.report_swap()
    """

    # Expose useragent for watchdog integration
    USERAGENT = MPV_USERAGENT

    def __init__(
            self,
            debounce_s: float = 0.150,
            tune_timeout_s: float = 20.0,
            max_retries: int = 2,
            frame_grace_s: float = 0.2,  # ignore early frames (buffered)
    ):
        self._mpv: EmbeddedMPV | None = None
        self.log = Logger("tuner")

        # Timing config
        self._debounce_s = debounce_s
        self._tune_timeout_s = tune_timeout_s
        self._max_retries = max_retries
        self._frame_grace_s = frame_grace_s

        # State
        self._state = TunerState.IDLE
        self._current_url: str | None = None
        self._current_name: str = ""

        # Pending request (debounced)
        self._pending_url: str | None = None
        self._pending_name: str = ""
        self._debounce_deadline: float = 0.0

        # Tune tracking
        self._tune_started_at: float = 0.0
        self._tune_attempts: int = 0
        self._status_message: str | None = None

    # -------------------------------------------------------------------------
    # Properties
    # -------------------------------------------------------------------------

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

    # -------------------------------------------------------------------------
    # Lifecycle (wraps mpv)
    # -------------------------------------------------------------------------

    def initialize(self, test_source: str | None = "av://lavfi:mandelbrot") -> None:
        """
        Initialize the video player.
        
        Call after pygame display is created (needs GL context).
        Optionally loads a test source and pauses.
        """
        if self._mpv is None:
            self._mpv = EmbeddedMPV()
        self._mpv.initialize()
        if test_source:
            self._mpv.loadfile(test_source)
            self._mpv.pause()

    def shutdown(self) -> None:
        """Shutdown the video player and release resources."""
        if self._mpv:
            self._mpv.shutdown()
            self._mpv = None

    def pause(self) -> None:
        """Pause video playback (for menu mode)."""
        if self._mpv:
            self._mpv.pause()

    def resume(self) -> None:
        """Resume video playback (entering video mode)."""
        if self._mpv:
            self._mpv.resume()

    def add_volume(self, delta: int) -> None:
        """Adjust volume by delta (positive = louder)."""
        if self._mpv:
            self._mpv.add_volume(delta)

    def get_volume(self) -> int:
        """Get current volume level (0-100)."""
        if self._mpv:
            return self._mpv.get_volume()
        return 0

    # -------------------------------------------------------------------------
    # Rendering
    # -------------------------------------------------------------------------

    def render_tick(self, width: int, height: int) -> TunerStatus:
        """
        Render a frame and tick the tuner state machine.
        
        Call once per frame in the render loop.
        
        Returns:
            TuneStatus with current state, any message, and whether a frame was rendered.
        """
        did_render = False
        if self._mpv:
            did_render = self._mpv.maybe_render(width, height)
            self._mpv.tick()  # process pending mpv commands

        # Run state machine
        status = self._tick_state(did_render)
        return TunerStatus(
            state=status.state,
            channel_name=status.channel_name,
            message=status.message,
            did_render=did_render,
        )

    def report_swap(self) -> None:
        """Notify mpv that a buffer swap occurred. Call after pygame.display.flip()."""
        if self._mpv:
            self._mpv.report_swap()

    # -------------------------------------------------------------------------
    # Tuning API
    # -------------------------------------------------------------------------

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

    def reload(self, reason: str = "") -> None:
        """
        Reload current URL (for watchdog recovery).
        """
        if not self._current_url or not self._mpv:
            return

        self.log.out(f"Reload: {reason}")
        self._mpv.stop()
        self._mpv.loadfile_now(self._current_url)
        self._tune_started_at = time.time()
        self._tune_attempts = 0
        self._state = TunerState.TUNING

    # -------------------------------------------------------------------------
    # Internal
    # -------------------------------------------------------------------------

    def _tick_state(self, did_render_frame: bool) -> TunerStatus:
        """
        Internal: tick the tune state machine.
        
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
                    if self._current_url and self._mpv:
                        self._mpv.loadfile_now(self._current_url)
                else:
                    self._state = TunerState.FAILED
                    self._status_message = "No signal"
                    self.log.out("Tune failed; max retries exceeded")

        return TunerStatus(
            state=self._state,
            channel_name=self._current_name,
            message=self._status_message,
        )

    def _fire_tune(self, url: str, name: str) -> None:
        """Actually start the tune."""
        self._current_url = url
        self._current_name = name
        self._tune_started_at = time.time()
        self._tune_attempts = 0
        self._state = TunerState.TUNING

        self.log.out(f"Tune to: {name}")
        if self._mpv:
            self._mpv.loadfile_now(url)
