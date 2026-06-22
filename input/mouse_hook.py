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

        self._ignore_next_move = threading.Event()

        # Edge friction timing state variables
        self.at_edge = False
        self.friction_timer = None

        # DPI scaling multiplier to handle monitor vs Android DPI difference
        self.dpi_scale = 0.4

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

    def set_android_resolution(self, width: int, height: int, density_dpi: int = 0):
        """
        Called when the Android client sends its real screen resolution and DPI via the
        INIT:width:height:densityDpi TCP handshake packet. Updates the virtual coordinate
        clamps and the dynamic DPI scaling multiplier used by the Infinite Treadmill.
        """
        with self.state_lock:
            self._android_width = max(1, width)
            self._android_height = max(1, height)
            if density_dpi > 0:
                self.dpi_scale = 96.0 / density_dpi
            else:
                self.dpi_scale = 0.4
        logger.info(f"Android virtual bounds updated to {width}x{height}, DPI {density_dpi} (dpi_scale = {self.dpi_scale:.3f})")

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

    def trap_cursor(self, x, y):
        """
        Initiates Infinite Treadmill mode: resets virtual android coordinates
        and teleports the physical cursor to the screen centre.
        Also starts the secondary suppressing listener to swallow PC clicks/scrolls.
        """
        with self.state_lock:
            self.cursor_on_android = True
            self.android_x = 0
            self.android_y = 0
            cx = self.center_x
            cy = self.center_y

        # Start secondary suppressing listener BEFORE teleporting so no click
        # that arrives during the teleport frame leaks to the PC.
        self._start_suppress_listener()

        self._ignore_next_move.set()
        self.mouse_controller.position = (cx, cy)
        logger.info(f"Treadmill initiated. Cursor centred at ({cx}, {cy}). Click suppression ON.")

    def untrap_cursor(self):
        """Unlocks the physical mouse cursor, returning normal movement to the user."""
        with self.state_lock:
            self.cursor_on_android = False
            self._keyboard_focused_on_android = False

        # Stop click/scroll suppression first so the user regains full PC control
        self._stop_suppress_listener()

        try:
            from input.keyboard_hook import KeyboardHookManager
            kb = KeyboardHookManager.active_instance
            if kb:
                kb.update_suppression_state(False)
        except Exception as e:
            logger.error(f"Error releasing keyboard on untrap: {e}")
        logger.info("Cursor untrapped. Click suppression OFF. Local control restored.")

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
        """Global mouse move event callback (called on pynput's listener thread)."""
        # ── FIX: use threading.Event.is_set() + clear() atomically ──────────
        if self._ignore_next_move.is_set():
            self._ignore_next_move.clear()
            return

        with self.state_lock:
            cursor_on_android = self.cursor_on_android
            cx = self.center_x
            cy = self.center_y

        if cursor_on_android:
            dx = x - cx
            dy = y - cy

            # Apply DPI scaling to physical deltas and convert to integers
            dx = int(dx * self.dpi_scale)
            dy = int(dy * self.dpi_scale)

            if dx != 0 or dy != 0:
                with self.state_lock:
                    # ── Bug 2 Fix: clamp to REAL device resolution from INIT handshake
                    aw = self._android_width
                    ah = self._android_height
                    # ── Bug 5 Fix: allow android_x to go as negative as
                    # -ESCAPE_BUFFER_PX before treating it as an escape.
                    # This prevents sensor jitter from firing the unlock.
                    self.android_x = max(-(ESCAPE_BUFFER_PX + 1),
                                         min(self.android_x + dx, aw))
                    self.android_y = max(0, min(self.android_y + dy, ah))
                    virtual_x = self.android_x

                # ── Bug 5 Fix: require deliberate leftward swipe (> ESCAPE_BUFFER_PX)
                # Natural jitter at x≈0 produces deltas of ±1–5px at most, which
                # will nudge android_x to -1..-5 — well within the buffer zone.
                # Only a sustained swipe accumulating > 30px of leftward virtual
                # movement triggers the escape.
                if virtual_x <= -ESCAPE_BUFFER_PX:
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

                # Forward delta to Android client
                if self.on_mouse_move_delta_callback:
                    try:
                        self.on_mouse_move_delta_callback(dx, dy)
                    except Exception as e:
                        logger.error(f"Error in mouse move delta callback: {e}")

                # ── FIX: set flag BEFORE teleporting ─────────────────────────
                self._ignore_next_move.set()
                # Outside the state_lock – X11 I/O must not be under a lock
                self.mouse_controller.position = (cx, cy)
            return

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
