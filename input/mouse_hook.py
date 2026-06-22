import logging
import threading
import subprocess
import re
from pynput import mouse
from pynput.mouse import Controller

logger = logging.getLogger("deskstream.mouse_hook")

def get_primary_monitor_resolution():
    """
    Attempts to detect the primary monitor resolution using multiple methods:
    1. Tkinter screen query (cross-platform, standard library)
    2. xrandr command parser (X11 Linux primary display specific)
    Defaults to 1920x1080 if detection fails.
    """
    # Method 1: Tkinter (Built-in standard library tool)
    try:
        import tkinter
        root = tkinter.Tk()
        root.withdraw()  # Don't show the window
        width = root.winfo_screenwidth()
        height = root.winfo_screenheight()
        root.destroy()
        if width > 0 and height > 0:
            logger.info(f"Primary monitor resolution detected via Tkinter: {width}x{height}")
            return width, height
    except Exception as e:
        logger.debug(f"Tkinter resolution query failed: {e}")

    # Method 2: xrandr (Linux specific, identifies primary display)
    try:
        output = subprocess.check_output(["xrandr"], stderr=subprocess.DEVNULL).decode()
        for line in output.splitlines():
            if "primary" in line:
                # Matches format like '1920x1080+0+0'
                match = re.search(r'\b(\d+)x(\d+)\+', line)
                if match:
                    w, h = int(match.group(1)), int(match.group(2))
                    logger.info(f"Primary monitor resolution detected via xrandr: {w}x{h}")
                    return w, h
    except Exception as e:
        logger.debug(f"xrandr resolution query failed: {e}")

    # Fallback to standard HD resolution
    logger.warning("Could not auto-detect screen resolution. Defaulting to 1920x1080.")
    return 1920, 1080


# ── Bug 5 fix: Escape Resistance Buffer ─────────────────────────────────────
# The virtual left-edge escape threshold (in virtual Android pixels).
# Requires android_x to go this far negative before the cursor is released
# back to the PC.  A value of 0 means ANY negative delta triggers escape,
# which makes natural sensor jitter (~±3 px) escape accidentally.
# 30px of deliberate leftward movement is required, matching approximately
# a 3cm wrist flick at 1000 DPI – clearly intentional, never accidental.
ESCAPE_BUFFER_PX = 30


class MouseHookManager:
    """
    Manages global mouse hooks, edge checking, edge friction, cursor confinement,
    and coordinate redirection (Infinite Treadmill mode) for DeskStream.

    FIX NOTES (catastrophic lockup prevention):
    - The primary mouse listener NEVER uses suppress=True.  The X11 mouse grab
      used by pynput's suppressed listener is a low-level XGrabPointer call that
      – once the listener thread dies – leaves the grab in place permanently,
      requiring REISUB to recover.  The primary listener is observe-only.
    - BUG 5 FIX: A secondary suppress=True listener (_suppress_listener) is
      started ONLY when cursor_on_android becomes True and stopped when it becomes
      False.  It covers only on_click and on_scroll (no on_move), silently
      consuming physical PC clicks/scrolls while the cursor is trapped on Android.
      It is always cleanly joined (with a 2s timeout) before being replaced or
      destroyed, and an atexit handler releases it unconditionally.
      Movement is NOT suppressed: we rely on the Treadmill teleport to keep the
      physical cursor locked to the screen centre; suppressing movement would
      require XGrabPointer on the primary listener which we explicitly avoid.
    - _ignore_next_move is a threading.Event (not a plain bool) to eliminate the
      race between the teleport write and the listener read.
    - The state_lock is never held while calling mouse_controller.position (X11 I/O).
    - stop() is guaranteed to reset all state even in the face of exceptions.
    """
    instance = None

    def __init__(self, settings_manager, on_screen_exit_callback=None,
                 on_mouse_move_delta_callback=None, on_mouse_click_callback=None,
                 on_mouse_scroll_callback=None, on_unlock_callback=None):
        """
        :param settings_manager: Instance of SettingsManager.
        :param on_screen_exit_callback: Called when cursor exits screen (edge, trap_x, trap_y).
        :param on_mouse_move_delta_callback: Called when cursor is trapped and moves (dx, dy).
        :param on_mouse_click_callback: Called when click detected while trapped (button, state).
        :param on_mouse_scroll_callback: Called when scroll wheel used while trapped (dy: int).
        :param on_unlock_callback: Called when user requests an unlock (cursor exits Android screen).
        """
        self.settings = settings_manager
        self.on_screen_exit_callback = on_screen_exit_callback
        self.on_mouse_move_delta_callback = on_mouse_move_delta_callback
        self.on_mouse_click_callback = on_mouse_click_callback
        self.on_mouse_scroll_callback = on_mouse_scroll_callback
        self.on_unlock_callback = on_unlock_callback

        self.mouse_controller = Controller()
        self.listener = None
        self.state_lock = threading.Lock()

        # Monitor layout dimensions
        width, height = get_primary_monitor_resolution()
        self._screen_width = width
        self._screen_height = height
        self.center_x = width // 2
        self.center_y = height // 2

        # Infinite Treadmill State Management
        self.cursor_on_android = False
        self._keyboard_focused_on_android = False
        self.android_x = 0
        self.android_y = 0

        # Android device resolution (set dynamically via INIT handshake)
        self._android_width = 1080
        self._android_height = 2400

        # ── Bug 6 Fix: Phantom Event Storm Counter ──────────────────────────
        # X11's async event queue can buffer multiple MotionNotify events BEFORE
        # the WarpPointer call is processed by the server.  A single-shot
        # threading.Event only absorbs the FIRST phantom event; subsequent ones
        # (typically 2-4 in practice) each carry the full edge→center delta
        # (e.g. raw_dx ≈ ±960 px) which at dpi_scale ≈ 3.33 injects ~3,200
        # Android pixels per leaked event — the "speed explosion" symptom.
        # An integer counter set to _TELEPORT_IGNORE_COUNT on every teleport
        # robustly drains the entire phantom storm depth without a race.
        self._ignore_move_count = 0
        self._ignore_next_move = threading.Event()  # kept for API compat; drives count reset

        # Edge friction timing state variables
        self.at_edge = False
        self.friction_timer = None

        # DPI scaling multiplier to handle monitor vs Android DPI difference
        # Scaled up by a default 1.3x speed boost factor to make the cursor feel slightly faster.
        self.dpi_scale = 1.3

        # ── User-facing sensitivity multiplier ────────────────────────────────
        # Applied on top of dpi_scale inside _on_move.  Kept separate so that
        # set_android_resolution() (which rewrites dpi_scale on every reconnect)
        # never silently resets the user's GUI preference.
        # Initialized from persisted settings; updated live via set_sensitivity().
        self.sensitivity_multiplier = self.settings.get_mouse_sensitivity()

        # ── Bug 3 Fix: Sub-pixel accumulator ─────────────────────────────────
        # When dpi_scale < 1.0, int(dx * dpi_scale) truncates the fractional
        # remainder every tick, losing up to 0.99 px per event.  At 200 Hz this
        # discards up to ~200 px/s of intended movement at low speeds.
        # We accumulate the fractional part and only send whole pixels.
        self._subpx_x = 0.0
        self._subpx_y = 0.0

        # ── Bug 5 Fix: secondary suppressing listener ─────────────────────────
        # Started when trapped (cursor_on_android=True), stopped when released.
        # Covers only on_click / on_scroll; movement is NOT suppressed.
        self._suppress_listener = None
        self._suppress_lock = threading.Lock()   # separate lock to avoid deadlock

        import atexit
        atexit.register(self._emergency_release_suppress)

        MouseHookManager.instance = self

    # ── Screen dimension properties ──────────────────────────────────────────

    @property
    def screen_width(self):
        with self.state_lock:
            return self._screen_width

    @screen_width.setter
    def screen_width(self, value):
        with self.state_lock:
            self._screen_width = value
            self.center_x = value // 2

    @property
    def screen_height(self):
        with self.state_lock:
            return self._screen_height

    @screen_height.setter
    def screen_height(self, value):
        with self.state_lock:
            self._screen_height = value
            self.center_y = value // 2

    # ── Keyboard-focus property ──────────────────────────────────────────────

    @property
    def keyboard_focused_on_android(self):
        with self.state_lock:
            return self._keyboard_focused_on_android

    @keyboard_focused_on_android.setter
    def keyboard_focused_on_android(self, value):
        with self.state_lock:
            if self._keyboard_focused_on_android == value:
                return
            self._keyboard_focused_on_android = value
            logger.info(f"keyboard_focused_on_android changed to: {value}")

        # Notify active KeyboardHookManager (outside lock to avoid deadlock)
        try:
            from input.keyboard_hook import KeyboardHookManager
            kb = KeyboardHookManager.active_instance
            if kb:
                kb.update_suppression_state(value)
        except Exception as e:
            logger.error(f"Error notifying KeyboardHookManager: {e}")

    # ── Android device resolution (dynamic via INIT handshake) ─────────────

    def set_sensitivity(self, value: float):
        """
        Live-updates the user-facing sensitivity multiplier.
        Thread-safe: acquires state_lock before mutating the field so _on_move
        never reads a partially written float.

        :param value: Desired multiplier. Clamped to [0.1, 3.0] for safety.
        """
        clamped = max(0.1, min(3.0, float(value)))
        with self.state_lock:
            self.sensitivity_multiplier = clamped
        logger.debug(f"Mouse sensitivity_multiplier set to {clamped:.2f}")

    def set_android_resolution(self, width: int, height: int, density_dpi: int = 0):
        """
        Called when the Android client sends its real screen resolution and DPI via the
        INIT:width:height:densityDpi TCP handshake packet. Updates the virtual coordinate
        clamps and the dynamic DPI scaling multiplier used by the Infinite Treadmill.

        DPI SCALE MATH:
          The PC mouse generates deltas in 96-DPI logical pixels (1 raw pixel = 1/96 inch).
          The Android screen has `density_dpi` physical pixels per inch.
          To make 1 inch of physical mouse travel move 1 inch on the Android glass, MULTIPLY:

              dpi_scale = density_dpi / 96.0

          Example – Redmi 9i (320 DPI): dpi_scale = 320 / 96 ≈ 3.33
            → 1 raw PC delta pixel → 3.33 Android pixels  (correct, same physical travel)

        RECONNECT SESSION BOUNDARY – "TOTAL BRAIN WIPE":
          Every INIT call is an unconditional session boundary.  Whether the previous
          session ended cleanly or via an abrupt TCP drop, this method resets the
          *complete* treadmill state before applying the new DPI math:

          1. cursor_on_android → False: The treadmill is disarmed immediately under the
             state_lock so _on_move() cannot race through the accumulator pipeline
             with stale values after the lock is released.
          2. _keyboard_focused_on_android → False: keyboard suppression is released.
          3. android_x / android_y → 0: virtual position wiped.
          4. _subpx_x / _subpx_y → 0.0: fractional accumulator flushed.
          5. _cancel_friction_timer_under_lock(): any pending edge-friction is cancelled.
          6. _ignore_next_move is armed AFTER the lock so the first post-INIT move
             event (which may carry a stale pre-teleport X11 delta) is discarded.
          7. The secondary suppress listener is torn down OUTSIDE the lock to avoid
             deadlock (it acquires _suppress_lock internally).
          8. Keyboard suppression is released via KeyboardHookManager OUTSIDE the
             state_lock to avoid cross-lock ordering issues.

          This makes INIT idempotent and safe to call on every reconnect, whether or
          not the previous session ended cleanly.
        """
        # ── Step 1-5: atomically disarm the treadmill and apply new DPI math ──────────
        was_trapped = False
        with self.state_lock:
            self._android_width = max(1, width)
            self._android_height = max(1, height)

            # Apply a 1.3x speed boost factor so the cursor feels snappier.
            speed_multiplier = 1.3
            if density_dpi > 0:
                self.dpi_scale = (density_dpi / 96.0) * speed_multiplier
            else:
                # No DPI info from handshake: use identity * speed_multiplier.
                self.dpi_scale = speed_multiplier

            # Full treadmill state wipe – covers both clean and dirty disconnects.
            if self.cursor_on_android:
                was_trapped = True          # remember so we can clean up outside lock
                self.cursor_on_android = False
            self._keyboard_focused_on_android = False
            self.android_x = 0
            self.android_y = 0
            self._subpx_x = 0.0
            self._subpx_y = 0.0
            # Cancel any pending edge-friction timers from the previous session.
            self._cancel_friction_timer_under_lock()

        # ── Step 6: clear the legacy single-shot Event ────────────────────────────────
        # Do NOT set it here — doing so races with trap_cursor()'s own .set() call
        # when the user immediately re-traps after reconnect.  The Event would be
        # consumed by the first phantom _on_move, leaving the remaining 2-5 X11
        # buffered phantom events to slip past both gates (the confirmed reconnect
        # speed-doubling root cause).  _ignore_move_count in trap_cursor() is the
        # sole authoritative phantom-drain mechanism; the Event is legacy API compat.
        self._ignore_next_move.clear()

        # ── Steps 7-8: teardown outside state_lock to avoid deadlock ─────────────────
        if was_trapped:
            # Stop the click/scroll suppression listener that was started by trap_cursor().
            self._stop_suppress_listener()
            # Release keyboard suppression via KeyboardHookManager.
            try:
                from input.keyboard_hook import KeyboardHookManager
                kb = KeyboardHookManager.active_instance
                if kb:
                    kb.update_suppression_state(False)
            except Exception as e:
                logger.error(f"set_android_resolution: error releasing keyboard suppression: {e}")
            logger.info("set_android_resolution: cursor was trapped — suppress listeners torn down.")

        logger.info(
            f"Android virtual bounds updated to {width}x{height}, DPI {density_dpi} "
            f"(dpi_scale = {self.dpi_scale:.3f}). "
            f"Full session state wiped (was_trapped={was_trapped})."
        )

    def on_client_disconnect(self):
        """
        Called by the TCP server layer whenever the Android client socket drops
        (graceful close OR network error).  This is the authoritative hook for
        cleaning up treadmill state on an unclean disconnect — i.e. the case where
        the client vanishes without sending an UNLOCK command first.

        WHY THIS EXISTS:
          Without this hook, cursor_on_android stays True after a TCP drop.  The
          pynput listener keeps running the treadmill loop (centre-teleport every
          _on_move tick), accumulating stale deltas that are never forwarded.
          When the client reconnects and sends INIT, set_android_resolution() wipes
          the accumulators — but between the drop and the INIT there is a window
          of hundreds of milliseconds where stale phantom motion can corrupt state.
          Calling on_client_disconnect() immediately on drop closes that window.

        IMPLEMENTATION:
          Delegates to untrap_cursor() so the full teardown path (suppress listener,
          keyboard hook notification, state reset) runs exactly once through the
          canonical code path, with no duplication.
        """
        logger.info("on_client_disconnect: TCP client dropped — forcing cursor untrap.")
        # Only untrap if we were actually trapped; untrap_cursor() is idempotent.
        with self.state_lock:
            is_trapped = self.cursor_on_android
        if is_trapped:
            self.untrap_cursor()
            # Notify the unlock callback so streaming is stopped in main.py
            if self.on_unlock_callback:
                try:
                    self.on_unlock_callback()
                except Exception as e:
                    logger.error(f"on_client_disconnect: error in on_unlock_callback: {e}")
        else:
            logger.debug("on_client_disconnect: cursor was not trapped, no-op.")

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def start(self):
        """Starts the primary observe-only mouse listener thread."""
        with self.state_lock:
            if self.listener is not None:
                logger.warning("Mouse listener thread is already active.")
                return

            # Primary listener: suppress=False (observe-only, NO XGrabPointer).
            # Physical movement, clicks, and scrolls still reach the OS normally.
            # Clicks/scrolls are swallowed by the secondary suppressing listener
            # (started in _start_suppress_listener) only while cursor_on_android.
            self.listener = mouse.Listener(
                on_move=self._on_move,
                on_click=self._on_click,
                on_scroll=self._on_scroll
            )
            self.listener.start()
            logger.info("Primary mouse listener started (observe-only).")

    def stop(self):
        """Stops both mouse listeners and resets all state unconditionally."""
        with self.state_lock:
            self._cancel_friction_timer_under_lock()
            self.cursor_on_android = False
            self._keyboard_focused_on_android = False
            self._ignore_next_move.clear()

        # Stop secondary suppressing listener first
        self._stop_suppress_listener()

        # Stop keyboard suppression (outside state_lock to avoid deadlock)
        try:
            from input.keyboard_hook import KeyboardHookManager
            kb = KeyboardHookManager.active_instance
            if kb:
                kb.update_suppression_state(False)
        except Exception as e:
            logger.error(f"Error releasing keyboard suppression on stop: {e}")

        with self.state_lock:
            if self.listener:
                try:
                    self.listener.stop()
                except Exception as e:
                    logger.error(f"Error stopping primary mouse listener: {e}")
                self.listener = None

        logger.info("Mouse listeners stopped. All state reset.")

    # ── Cursor trap / untrap ─────────────────────────────────────────────────

    # Number of _on_move events to discard after each teleport.
    # Set conservatively to 6 to cover the deepest observed X11 phantom-event
    # storm (typically 2-4 events; 6 adds margin for high-latency GPU compositors).
    _TELEPORT_IGNORE_COUNT = 6

    def trap_cursor(self, x, y):
        """
        Initiates Infinite Treadmill mode: resets virtual android coordinates
        and teleports the physical cursor to the screen centre.
        Also starts the secondary suppressing listener to swallow PC clicks/scrolls.

        BUG 6 FIX — Phantom Event Storm:
          After mouse_controller.position = (cx, cy) the X11 server enqueues a
          WarpPointer command, but MotionNotify events already buffered in the
          XEvent queue for the pre-warp position are delivered FIRST.  These
          carry the full edge→center delta and each one would inject thousands
          of Android pixels if not suppressed.  The previous single-shot
          threading.Event only blocked the FIRST phantom event.

          Fix: _ignore_move_count is set to _TELEPORT_IGNORE_COUNT (6) before
          each teleport and decremented by _on_move until it reaches zero.
          The jitter guard (abs(raw_dx) > sw//2) is kept as a belt-and-suspenders
          second line of defence for any edge case where the counter runs out
          before the storm fully drains.
        """
        with self.state_lock:
            self.cursor_on_android = True
            self.android_x = 0
            self.android_y = 0
            # Reset sub-pixel accumulator on every trap so stale fractional
            # remainders from the previous session never corrupt the first event
            # of the new session.  (The accumulator is always bounded to (-1, 1)
            # during operation, but an explicit reset is cleaner and safer.)
            self._subpx_x = 0.0
            self._subpx_y = 0.0
            # Arm the phantom-event drain counter UNDER the lock so _on_move
            # cannot race between reading cursor_on_android=True and seeing the
            # counter still at zero.
            self._ignore_move_count = self._TELEPORT_IGNORE_COUNT
            cx = self.center_x
            cy = self.center_y

        # Start secondary suppressing listener BEFORE teleporting so no click
        # that arrives during the teleport frame leaks to the PC.
        self._start_suppress_listener()

        # _ignore_move_count (set above under the lock) is the sole phantom-drain
        # mechanism.  Do NOT also set _ignore_next_move here: doing so creates a
        # double-arm race with set_android_resolution()'s .clear() path — on
        # reconnect the Event would be armed here, consumed by the FIRST phantom
        # _on_move invocation, leaving phantoms 2-5 to pass both gates.  Keep the
        # Event cleared so only the counter drains the post-teleport storm.
        self._ignore_next_move.clear()
        self.mouse_controller.position = (cx, cy)
        logger.info(
            f"Treadmill initiated. Cursor centred at ({cx}, {cy}). "
            f"Click suppression ON. Ignoring next {self._TELEPORT_IGNORE_COUNT} move events."
        )

    def untrap_cursor(self):
        """
        Unlocks the physical mouse cursor, returning normal movement to the user.

        BUG 6 FIX — Accumulator State Leak:
          Previously, _subpx_x/_subpx_y and android_x/android_y were NOT zeroed
          here.  This meant that any sub-pixel fractional residue accumulated
          during the current trap session, and the last virtual cursor position,
          survived into the next trap session if trap_cursor() was called quickly
          enough for _subpx_* to not have been reset yet.  While trap_cursor()
          also zeroes these, the window between untrap and the next trap is a
          state-leak hazard — especially when untrap is triggered externally
          (e.g. on_client_disconnect) rather than from the escape branch.

          Fix: zero all kinematic state atomically under state_lock on every
          untrap so the system is always in a clean, known-good state between
          sessions, regardless of which code path triggered the untrap.

          _ignore_move_count is also reset to 0 so no phantom drain counter
          from a previous teleport can suppress real post-untrap move events.
        """
        with self.state_lock:
            self.cursor_on_android = False
            self._keyboard_focused_on_android = False
            # Full kinematic wipe — eliminates accumulator state bleed between sessions.
            self.android_x = 0
            self.android_y = 0
            self._subpx_x = 0.0
            self._subpx_y = 0.0
            self._ignore_move_count = 0
            self._ignore_next_move.clear()

        # Stop click/scroll suppression first so the user regains full PC control
        self._stop_suppress_listener()

        try:
            from input.keyboard_hook import KeyboardHookManager
            kb = KeyboardHookManager.active_instance
            if kb:
                kb.update_suppression_state(False)
        except Exception as e:
            logger.error(f"Error releasing keyboard on untrap: {e}")
        logger.info("Cursor untrapped. Click suppression OFF. Local control restored. Kinematic state wiped.")

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _cancel_friction_timer_under_lock(self):
        """Cancels any running edge friction timer. Caller must hold state_lock."""
        if self.friction_timer:
            self.friction_timer.cancel()
            self.friction_timer = None
        self.at_edge = False

    # ── Bug 5: secondary suppressing listener lifecycle ──────────────────────

    def _suppress_noop_click(self, x, y, button, pressed):
        """Suppressing listener click callback: do nothing, event is consumed."""
        logger.debug(f"[suppress] Ate PC click: {button} pressed={pressed}")

    def _suppress_noop_scroll(self, x, y, dx, dy):
        """Suppressing listener scroll callback: do nothing, event is consumed."""
        logger.debug(f"[suppress] Ate PC scroll: dx={dx} dy={dy}")

    def _start_suppress_listener(self):
        """
        Starts a suppress=True mouse listener that covers only clicks and scrolls.
        Called from trap_cursor().  Any previously running suppress listener is
        cleanly stopped first.
        """
        with self._suppress_lock:
            self._stop_suppress_listener_under_lock()
            try:
                sl = mouse.Listener(
                    on_click=self._suppress_noop_click,
                    on_scroll=self._suppress_noop_scroll,
                    suppress=True
                )
                sl.start()
                self._suppress_listener = sl
                logger.info("Secondary suppress listener started (clicks/scrolls swallowed).")
            except Exception as e:
                logger.error(f"Failed to start suppress listener: {e}")
                self._suppress_listener = None

    def _stop_suppress_listener(self):
        """Stops the secondary suppressing listener. Safe to call from any thread."""
        with self._suppress_lock:
            self._stop_suppress_listener_under_lock()

    def _stop_suppress_listener_under_lock(self):
        """Caller must hold self._suppress_lock."""
        sl = self._suppress_listener
        if sl is None:
            return
        self._suppress_listener = None
        try:
            sl.stop()
            if sl.is_alive():
                sl.join(timeout=2.0)
                if sl.is_alive():
                    logger.warning("Suppress listener thread did not exit within 2s.")
        except Exception as e:
            logger.debug(f"Error stopping suppress listener: {e}")
        logger.info("Secondary suppress listener stopped.")

    def _emergency_release_suppress(self):
        """atexit handler: unconditionally releases the suppress listener's X11 grab."""
        try:
            self._stop_suppress_listener()
        except Exception:
            pass

    def _on_friction_timer_fired(self, edge, x, y):
        """Invoked when the user has held the cursor at the edge past the friction threshold."""
        with self.state_lock:
            # Re-verify we are still at the edge and not already trapped
            if not self.at_edge or self.cursor_on_android:
                return
            logger.info(f"Friction boundary threshold crossed on the {edge} edge.")
            self._cancel_friction_timer_under_lock()

        # Determine the exact locking coordinate on the edge
        trap_x, trap_y = x, y
        if edge == "RIGHT":
            trap_x = self._screen_width - 1
        elif edge == "LEFT":
            trap_x = 0
        elif edge == "TOP":
            trap_y = 0
        elif edge == "BOTTOM":
            trap_y = self._screen_height - 1

        if self.on_screen_exit_callback:
            try:
                self.on_screen_exit_callback(edge, trap_x, trap_y)
            except Exception as e:
                logger.error(f"Error executing on_screen_exit callback: {e}")

    def _check_edge(self, x, y):
        """Checks whether (x,y) is on the configured boundary edge."""
        edge = self.settings.get_selected_edge()
        is_hit = False
        sw = self._screen_width
        sh = self._screen_height

        if edge == "RIGHT" and x >= sw - 1:
            is_hit = True
        elif edge == "LEFT" and x <= 0:
            is_hit = True
        elif edge == "TOP" and y <= 0:
            is_hit = True
        elif edge == "BOTTOM" and y >= sh - 1:
            is_hit = True

        return is_hit, edge

    # ── pynput callbacks ─────────────────────────────────────────────────────

    def _on_move(self, x, y):
        """
        Global mouse move event callback (called on pynput's listener thread).

        THREAD-SAFETY ARCHITECTURE:
          All mutable treadmill state (cursor_on_android, center coords, dpi_scale,
          android dimensions, android_x/y, _subpx_x/_subpx_y) is read and written
          inside a SINGLE unified `with self.state_lock` block.  This eliminates the
          previous TOCTOU race where:
            - Block 1 read cursor_on_android + cx/cy
            - Block 2 read _screen_width/_screen_height  (separate acquire/release)
            - Block 3 read dpi_scale + accumulate _subpx_x/y  (separate acquire/release)
            - Block 4 read aw/ah + update android_x/y  (separate acquire/release)
          Between each pair of lock releases, the TCP thread could call
          set_android_resolution() and mutate dpi_scale, _subpx_*, or android_*,
          producing a mixed-state snapshot that caused the speed explosion.

          The unified block reads cursor_on_android, and if False, exits immediately —
          so the overhead on the non-trapped hot path is a single fast lock+read+unlock.

          X11 I/O (mouse_controller.position) is still performed OUTSIDE the lock,
          as blocking I/O under a mutex is forbidden by the design contract.
        """
        # ── Phantom-event drain counter gate ─────────────────────────────────────────
        # _ignore_move_count is set to _TELEPORT_IGNORE_COUNT by trap_cursor() under
        # state_lock immediately before each WarpPointer call.  It drains one count
        # per _on_move invocation, blocking the entire X11 phantom-event storm that
        # precedes the warp taking effect.  Using an integer counter instead of a
        # single-shot threading.Event absorbs ALL buffered pre-warp MotionNotify
        # events (typically 2-4, capped at 6 for margin).
        #
        # NOTE: This check reads _ignore_move_count WITHOUT the state_lock because:
        #   (a) it is only ever written by trap_cursor() (under lock) and here.
        #   (b) the only risk is a torn read between trap() setting it to 6 and us
        #       reading 0 — in which case we fall through to the jitter guard below,
        #       which is a safe belt-and-suspenders fallback.
        #   (c) Holding state_lock here on the hot-path (200 Hz) would invert our
        #       lock-ordering contract (Event gate runs BEFORE the unified block).
        if self._ignore_move_count > 0:
            self._ignore_move_count -= 1
            logger.debug(f"_on_move: phantom event suppressed (remaining={self._ignore_move_count}).")
            return
        # Legacy single-shot gate — kept for any caller that arms it directly.
        if self._ignore_next_move.is_set():
            self._ignore_next_move.clear()
            return

        # ── Unified atomic state snapshot + accumulator pipeline ──────────────────────
        # All state reads and writes happen inside ONE lock acquisition.
        # This is the critical fix: cursor_on_android, dpi_scale, _subpx_*,
        # android_x/y are all read from a single coherent snapshot, so a concurrent
        # set_android_resolution() or on_client_disconnect() cannot interleave and
        # create a mixed-generation state (old dpi_scale + reset accumulator, etc.).
        dx = 0
        dy = 0
        cx = 0
        cy = 0
        virtual_x = 0
        should_forward = False
        should_escape = False

        with self.state_lock:
            cursor_on_android = self.cursor_on_android
            if not cursor_on_android:
                # Not trapped — fall through to edge detection below.
                sw = self._screen_width
                sh = self._screen_height
            else:
                cx = self.center_x
                cy = self.center_y
                raw_dx = x - cx
                raw_dy = y - cy

                # ── Teleport-jitter guard ─────────────────────────────────────
                # When trap_cursor() teleports the physical cursor from the screen
                # edge (e.g. x=1919) to the centre (e.g. x=960), X11 is async.
                # pynput may fire 1–3 more move events with the OLD pre-teleport
                # coordinates before the OS catches up.  _ignore_next_move is
                # single-shot so only the FIRST is absorbed by the gate above.
                # Remaining phantom events see raw_dx = edge - centre ≈ ±960 px,
                # which at dpi_scale ≈ 3.33 becomes ~3200 Android pixels — the
                # "violently fast" symptom.  Guard: any delta exceeding half the
                # screen is physically impossible from real hardware and is jitter.
                _sw = self._screen_width
                _sh = self._screen_height
                if abs(raw_dx) > _sw // 2 or abs(raw_dy) > _sh // 2:
                    # Belt-and-suspenders: a large delta that slipped past the
                    # counter gate (e.g. counter exhausted before storm drained).
                    # Re-arm the counter for one more event so the next phantom
                    # is also caught, and log at WARNING so we can tune the count.
                    self._ignore_move_count = max(self._ignore_move_count, 1)
                    logger.warning(
                        f"_on_move: teleport-jitter slipped past counter "
                        f"(raw_dx={raw_dx}, raw_dy={raw_dy}). "
                        f"Counter reset to {self._ignore_move_count}. "
                        f"Consider increasing _TELEPORT_IGNORE_COUNT."
                    )
                    return  # exits the `with` block cleanly

                # ── Sub-pixel accumulator pipeline (pure linear scaling) ───────
                # Pipeline per axis:
                #   effective = dpi_scale * sensitivity_multiplier
                #   scaled    = raw_delta * effective       e.g. 10 × 3.33 × 1.5 = 49.9
                #   int_out   = int(accum + scaled)         e.g. 49
                #   accum     = (accum + scaled) - int_out   e.g. 0.9 → next tick
                # sensitivity_multiplier is read under this same lock so it is
                # always coherent with dpi_scale in the same snapshot.
                effective_scale = self.dpi_scale * self.sensitivity_multiplier
                self._subpx_x += raw_dx * effective_scale
                self._subpx_y += raw_dy * effective_scale
                dx = int(self._subpx_x)
                dy = int(self._subpx_y)
                self._subpx_x -= dx
                self._subpx_y -= dy

                if dx != 0 or dy != 0:
                    # ── Virtual coordinate clamping + escape detection ──────────
                    # Clamp to real device resolution received via INIT handshake.
                    # Allow android_x to go as negative as -(ESCAPE_BUFFER_PX+1)
                    # before treating it as a deliberate escape swipe — prevents
                    # natural sensor jitter (~±3px) from firing an accidental unlock.
                    aw = self._android_width
                    ah = self._android_height
                    self.android_x = max(-(ESCAPE_BUFFER_PX + 1),
                                         min(self.android_x + dx, aw))
                    self.android_y = max(0, min(self.android_y + dy, ah))
                    virtual_x = self.android_x

                    if virtual_x <= -ESCAPE_BUFFER_PX:
                        should_escape = True
                    else:
                        should_forward = True
        # ── End of unified lock block ─────────────────────────────────────────────────

        if not cursor_on_android:
            # ── Normal (non-trapped) edge detection ──────────────────────────
            is_hit, edge = self._check_edge(x, y)
            if is_hit:
                with self.state_lock:
                    if not self.at_edge:
                        self.at_edge = True
                        friction_ms = self.settings.get_edge_friction_ms()
                        logger.debug(f"Mouse reached boundary ({edge} edge). "
                                     f"Starting friction timer for {friction_ms}ms.")
                        self.friction_timer = threading.Timer(
                            friction_ms / 1000.0,
                            self._on_friction_timer_fired,
                            args=[edge, x, y]
                        )
                        self.friction_timer.start()
            else:
                with self.state_lock:
                    if self.at_edge:
                        logger.debug(f"Mouse moved away from {edge} edge. Cancelling friction timer.")
                        self._cancel_friction_timer_under_lock()
            return

        # ── Treadmill mode: process the outcome flags set inside the lock ──────────────
        if should_escape:
            logger.info(
                f"Virtual boundary breached (android_x={virtual_x} ≤ "
                f"-{ESCAPE_BUFFER_PX}). Unlocking cursor."
            )
            self.untrap_cursor()
            if self.on_unlock_callback:
                try:
                    self.on_unlock_callback()
                except Exception as e:
                    logger.error(f"Error executing on_unlock_callback: {e}")
            return

        if should_forward:
            # Forward delta to Android client
            if self.on_mouse_move_delta_callback:
                try:
                    self.on_mouse_move_delta_callback(dx, dy)
                except Exception as e:
                    logger.error(f"Error in mouse move delta callback: {e}")

            # Re-arm the phantom-event drain counter BEFORE every mid-session
            # teleport.  This is the critical fix for the reconnect speed-doubling
            # bug: previously only _ignore_next_move (single-shot) was re-armed
            # here, absorbing exactly 1 phantom per tick.  X11 typically delivers
            # 2-4 buffered MotionNotify events per WarpPointer, so the remaining
            # 1-3 each injected the full edge→center delta (~960px raw × dpi_scale
            # ≈ 3200 Android px) on every tick — appearing as compounding speed.
            # Re-arming the integer counter ensures ALL phantoms are drained,
            # consistent with how trap_cursor() arms it on session start.
            # NOTE: We use max() to avoid shrinking a counter already in-flight
            # (possible if a previous phantom storm hasn't fully drained yet).
            self._ignore_move_count = max(
                self._ignore_move_count, self._TELEPORT_IGNORE_COUNT
            )
            # X11 I/O outside the lock — blocking calls under a mutex are forbidden.
            self.mouse_controller.position = (cx, cy)

        # ── Normal (non-trapped) edge detection ──────────────────────────────
        # Read dimensions without the lock to keep the hot path lean
        with self.state_lock:
            sw = self._screen_width
            sh = self._screen_height

        is_hit, edge = self._check_edge(x, y)

        if is_hit:
            with self.state_lock:
                if not self.at_edge:
                    self.at_edge = True
                    friction_ms = self.settings.get_edge_friction_ms()
                    logger.debug(f"Mouse reached boundary ({edge} edge). "
                                 f"Starting friction timer for {friction_ms}ms.")
                    self.friction_timer = threading.Timer(
                        friction_ms / 1000.0,
                        self._on_friction_timer_fired,
                        args=[edge, x, y]
                    )
                    self.friction_timer.start()
        else:
            with self.state_lock:
                if self.at_edge:
                    logger.debug(f"Mouse moved away from {edge} edge. Cancelling friction timer.")
                    self._cancel_friction_timer_under_lock()

    def _on_click(self, x, y, button, pressed):
        """Global mouse click event callback.

        BUG 4 FIX – Scroll Wheel Triggering Clicks:
        On Linux X11, pynput maps the scroll wheel to Button.button4 (up) and
        Button.button5 (down) and fires them through on_click rather than
        on_scroll in some driver configurations.  Without filtering, these reach
        the Android client as malformed C:BUTTON.BUTTON4:1 packets.  Even though
        the Kotlin handler ignores unknown button names, the spurious _on_click
        invocation races with the legitimate _on_scroll callback and can corrupt
        the drag-state machine (e.g. flipping isLeftButtonDown via a bad timing
        window).  We filter them out here before any state is touched.
        """
        # ── Bug 4 Fix: reject scroll-wheel pseudo-buttons ─────────────────────
        # pynput names scroll buttons Button.button4 / Button.button5.
        # Any Button whose name is not one of the three real mouse buttons is a
        # hardware axis (scroll, tilt-wheel, extra thumb buttons) and must NOT
        # be forwarded as a click event.
        REAL_BUTTONS = {mouse.Button.left, mouse.Button.right, mouse.Button.middle}
        if button not in REAL_BUTTONS:
            logger.debug(f"_on_click: ignoring non-standard button {button} (scroll/axis).")
            return

        with self.state_lock:
            cursor_on_android = self.cursor_on_android

        # Click-to-Focus: left-click while on Android focuses the keyboard
        is_left_click = (button == mouse.Button.left)
        if is_left_click and pressed:
            # Only update focus on button-down to avoid toggling on release
            self.keyboard_focused_on_android = cursor_on_android

        if cursor_on_android:
            state = 1 if pressed else 0
            if self.on_mouse_click_callback:
                try:
                    self.on_mouse_click_callback(button, state)
                except Exception as e:
                    logger.error(f"Error executing on_mouse_click callback: {e}")
            logger.debug(f"Click event forwarded: Button={button}, Pressed={pressed}")

    def _on_scroll(self, x, y, dx, dy):
        """Global mouse scroll event callback."""
        with self.state_lock:
            cursor_on_android = self.cursor_on_android

        if cursor_on_android:
            # dy from pynput: positive = scroll up, negative = scroll down.
            # We normalise: positive scroll_dy sent to Android = scroll down
            # (consistent with S:dy protocol where dy>0 = scroll down).
            scroll_dy = -dy  # flip sign: pynput up(+1) → Android down(+1)
            if self.on_mouse_scroll_callback:
                try:
                    self.on_mouse_scroll_callback(scroll_dy)
                except Exception as e:
                    logger.error(f"Error forwarding scroll delta: {e}")
            logger.debug(f"Scroll forwarded: raw dy={dy}, sent scroll_dy={scroll_dy}")
