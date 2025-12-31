"""
Tuner: high-level video tuning interface.

Encapsulates:
- mpv lifecycle (init, render, shutdown)
- Input debouncing (coalesces rapid channel changes)
- Tune timeout and retry logic
- State machine (IDLE → TUNING → PLAYING or FAILED)
- Stream health monitoring (watchdog)
"""
import time
from dataclasses import dataclass
from enum import Enum, auto
from queue import Empty
from typing import TYPE_CHECKING

from fptv.log import Logger
from fptv.mpv import EmbeddedMPV, MPV_USERAGENT
from fptv.tvh import WatchdogWorker

if TYPE_CHECKING:
    pass


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


class Tuner:
    """
    High-level video tuning controller.
    
    Owns and hides:
    - EmbeddedMPV (video player)
    - WatchdogWorker (stream health monitoring)
    
    Handles:
    - mpv lifecycle (initialize, render, shutdown)
    - Input debouncing (coalesces rapid channel changes)
    - Tune timeout and retry logic
    - State machine (IDLE → TUNING → PLAYING or FAILED)
    - Automatic stream recovery via watchdog
    
    Usage:
        tuner = Tuner(tvh)
        tuner.initialize()
        
        # Enter video mode
        tuner.resume()
        tuner.tune_now(url, "Channel: PBS")
        
        # In render loop:
        did_render = tuner.render_frame(width, height)
        status = tuner.tick(did_render)
        if did_render:
            pygame.display.flip()
            tuner.report_swap()
    """

    # Expose useragent for external use if needed
    USERAGENT = MPV_USERAGENT

    def __init__(
            self,
            tvh: "TVHeadendScanner | None" = None,
            debounce_s: float = 0.150,
            tune_timeout_s: float = 20.0,
            max_retries: int = 2,
            frame_grace_s: float = 0.2,  # ignore early frames (buffered)
    ):
        self._tvh = tvh
        self._mpv: EmbeddedMPV | None = None
        self._watchdog: WatchdogWorker | None = None
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
        Initialize the video player and watchdog.

        Call after pygame display is created (needs GL context).
        Optionally loads a test source and pauses.
        """
        if self._mpv is None:
            self._mpv = EmbeddedMPV()
        self._mpv.initialize()
        if test_source:
            self._mpv.loadfile(test_source)
            self._mpv.pause()

        # Start watchdog if we have a TVHeadend connection
        if self._tvh and self._watchdog is None:
            self._watchdog = WatchdogWorker(self._tvh, ua_tag=MPV_USERAGENT, interval_s=1.0)
            self._watchdog.start()

    def shutdown(self) -> None:
        """Shutdown the video player, watchdog, and release resources."""
        if self._watchdog:
            self._watchdog.shutdown()
            self._watchdog = None
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

    def render_frame(self, width: int, height: int) -> bool:
        """
        Render a video frame to the current GL context.

        Call once per frame in the render loop, before tick().

        Returns:
            True if a new frame was rendered, False otherwise.
        """
        if not self._mpv:
            return False
        return self._mpv.maybe_render(width, height)

    def tick(self, did_render_frame: bool = False) -> TunerStatus:
        """
        Tick the tuner state machine and process pending commands.

        Call once per frame, after render_frame().

        Args:
            did_render_frame: True if render_frame() returned True this tick

        Returns:
            TunerStatus with current state and any message for display.
        """
        if self._mpv:
            self._mpv.tick()  # process pending mpv commands

        # Process watchdog actions
        self._process_watchdog()

        # Run state machine
        return self._tick_state(did_render_frame)

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

    def _process_watchdog(self) -> None:
        """Update watchdog state and process any recovery actions."""
        if not self._watchdog:
            return

        # Update watchdog with current state
        self._watchdog.expecting = self.is_expecting_video
        self._watchdog.current_url = self._current_url
        self._watchdog.tuning_started_at = self._tune_started_at

        # Drain and process watchdog actions
        while True:
            try:
                action, url, reason = self._watchdog.actions.get_nowait()
            except Empty:
                break

            if action == "reload" and url:
                self.reload(reason)

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
