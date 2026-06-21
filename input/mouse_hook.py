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


class MouseHookManager:
    """
    Manages global mouse hooks, edge checking, edge friction, cursor confinement,
    and coordinate redirection (Infinite Treadmill mode) for DeskStream.

    FIX NOTES (catastrophic lockup prevention):
    - The mouse listener NEVER uses suppress=True.  The X11 mouse grab used by
      pynput's suppressed listener is a low-level XGrabPointer call that – once
      the Python callback raises an exception or the owning thread deadlocks –
      leaves the grab in place with no automatic release.  Result: the OS sees
      every button event "eaten" by the dead grab and the pointer is effectively
      frozen.  We avoid this entirely: the listener just observes events; we
      manipulate position ourselves via Controller.position.
    - _ignore_next_move is a threading.Event (not a plain bool) so the race
      between the teleport write on the main thread and the listener read on the
      pynput thread is eliminated.
    - The state_lock is never held while calling mouse_controller.position (which
      can block on X11 I/O), preventing a deadlock between the listener thread
      and the teleport setter.
    - stop() is guaranteed to untrap the cursor and reset all state even in the
      face of exceptions.
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

        # ── Bug 2 Fix: Android device resolution (set dynamically via INIT handshake)
        # Defaults are wide enough to avoid accidental boundary triggers before
        # the real resolution arrives.  The Android client sends INIT:w:h on connect.
        self._android_width = 1080
        self._android_height = 2400

        # ── FIX: Use a threading.Event instead of a plain bool ──────────────
        self._ignore_next_move = threading.Event()

        # Edge friction timing state variables
        self.at_edge = False
        self.friction_timer = None

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

    def set_android_resolution(self, width: int, height: int):
        """
        Called when the Android client sends its real screen resolution via the
        INIT:width:height TCP handshake packet.  Updates the virtual coordinate
        clamps used by the Infinite Treadmill so they match the actual device.
        """
        with self.state_lock:
            self._android_width = max(1, width)
            self._android_height = max(1, height)
        logger.info(f"Android virtual bounds updated to {width}x{height}")

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def start(self):
        """Starts the global mouse listener thread (NO suppress=True – see class docstring)."""
        with self.state_lock:
            if self.listener is not None:
                logger.warning("Mouse listener thread is already active.")
                return

            # ── FIX: suppress=False (default) ───────────────────────────────
            # We NEVER suppress mouse events at the listener level.
            # Suppression via XGrabPointer leaves a dangling X11 grab if the
            # listener thread dies, causing complete mouse/click lockout that
            # survives even program termination and requires REISUB to clear.
            self.listener = mouse.Listener(
                on_move=self._on_move,
                on_click=self._on_click,
                on_scroll=self._on_scroll
                # suppress=False (intentionally omitted / left at default)
            )
            self.listener.start()
            logger.info("Global mouse tracking listener started (observe-only, no X11 grab).")

    def stop(self):
        """Stops the global mouse listener and resets all state unconditionally."""
        # ── FIX: reset state BEFORE stopping the listener so the callbacks
        # that may fire during teardown see the correct state.
        with self.state_lock:
            self._cancel_friction_timer_under_lock()
            self.cursor_on_android = False
            self._keyboard_focused_on_android = False
            self._ignore_next_move.clear()

        # Stop keyboard suppression first (outside state_lock to avoid deadlock)
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
                    logger.error(f"Error stopping mouse listener: {e}")
                self.listener = None

        logger.info("Global mouse tracking listener stopped. All state reset.")

    # ── Cursor trap / untrap ─────────────────────────────────────────────────

    def trap_cursor(self, x, y):
        """
        Initiates Infinite Treadmill mode: resets virtual android coordinates
        and teleports the physical cursor to the screen centre.
        """
        with self.state_lock:
            self.cursor_on_android = True
            self.android_x = 0
            self.android_y = 0
            cx = self.center_x
            cy = self.center_y

        # ── FIX: set the Event BEFORE moving the cursor ──────────────────────
        # The listener thread fires _on_move almost immediately after the
        # position assignment below.  Setting the flag first guarantees the
        # listener sees it and skips the synthesised teleport event.
        self._ignore_next_move.set()
        # This call may block briefly on X11 – safe to do outside state_lock
        self.mouse_controller.position = (cx, cy)
        logger.info(f"Infinite Treadmill initiated. Physical cursor centred at ({cx}, {cy})")

    def untrap_cursor(self):
        """Unlocks the physical mouse cursor, returning normal movement to the user."""
        with self.state_lock:
            self.cursor_on_android = False
            self._keyboard_focused_on_android = False
        # Notify keyboard hook
        try:
            from input.keyboard_hook import KeyboardHookManager
            kb = KeyboardHookManager.active_instance
            if kb:
                kb.update_suppression_state(False)
        except Exception as e:
            logger.error(f"Error releasing keyboard on untrap: {e}")
        logger.info("Cursor untrapped. Local control restored.")

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _cancel_friction_timer_under_lock(self):
        """Cancels any running edge friction timer. Caller must hold state_lock."""
        if self.friction_timer:
            self.friction_timer.cancel()
            self.friction_timer = None
        self.at_edge = False

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

            if dx != 0 or dy != 0:
                with self.state_lock:
                    # ── Bug 2 Fix: clamp to REAL device resolution from INIT handshake
                    aw = self._android_width
                    ah = self._android_height
                    self.android_x = max(-1, min(self.android_x + dx, aw))
                    self.android_y = max(0, min(self.android_y + dy, ah))
                    virtual_x = self.android_x

                # Virtual left-edge breach → release cursor back to PC
                if virtual_x < 0:
                    logger.info("Virtual boundary breached (android_x < 0). Unlocking cursor.")
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
        """Global mouse click event callback."""
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
