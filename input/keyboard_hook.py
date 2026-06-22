import logging
import threading
from pynput import keyboard

logger = logging.getLogger("deskstream.keyboard_hook")


class KeyboardHookManager:
    """
    Manages global keyboard capturing for DeskStream Sync.

    ── Architecture: Dual-Listener Pattern ──────────────────────────────────────
    This class uses the same dual-listener design as MouseHookManager, adapted for
    keyboard events.

    LISTENER 1 – _observe_listener (suppress=False, always running while started):
      An observe-only (no XGrabKeyboard) listener on the primary keyboard.
      It fires _on_press / _on_release for every keypress.  These callbacks check
      `keyboard_focused_on_android` and forward the event over TCP if focused.
      Because suppress=False, it NEVER grabs the X11 keyboard; it can safely run
      at all times without any lockup risk.

    LISTENER 2 – _suppress_listener (suppress=True, only while focused=True):
      A secondary listener started ONLY when `update_suppression_state(True)` is
      called (i.e. the user left-clicked on the Android cursor focus area).
      Its callbacks are no-ops; its sole purpose is to consume the X11 key events
      so they do NOT reach the focused PC application (the "leak" bug).
      It is always cleanly stopped (with join) before a new one is started.

    WHY THIS FIXES THE LEAK:
      The old single-listener design relied on the suppress=True flag on the
      SAME listener that called the forward callback.  The deadlock happened because
      _on_press (running on the listener thread) spawned a daemon thread calling
      stop() → join(), which waited for... the listener thread itself.  Infinite wait.

      In the dual model:
        • _observe_listener fires _on_press (suppress=False → no XGrab, can be called
          from any context safely, including from within the listener thread).
        • _suppress_listener is a completely separate thread. Stopping it from
          _on_press is safe because _on_press is on the OBSERVE listener thread,
          not the SUPPRESS listener thread.  join() on the suppress listener will
          always succeed because no listener is waiting for itself.

    ── XGrabKeyboard safety ─────────────────────────────────────────────────────
    - _suppress_listener is always stopped (with a 2-second join) before a new one
      is started, preventing double-grab undefined behaviour.
    - An atexit handler unconditionally stops the suppress listener even if the
      process exits abnormally (crash, SIGKILL-adjacent, etc.).
    - _stop_suppress_listener() is safe to call from ANY thread, including from
      within the observe listener's callback thread.
    """

    SPECIAL_KEY_MAP = {
        keyboard.Key.backspace:  "BACKSPACE",
        keyboard.Key.enter:      "ENTER",
        keyboard.Key.tab:        "TAB",
        keyboard.Key.esc:        "ESCAPE",
        keyboard.Key.left:       "LEFT",
        keyboard.Key.right:      "RIGHT",
        keyboard.Key.up:         "UP",
        keyboard.Key.down:       "DOWN",
        keyboard.Key.delete:     "DELETE",
        keyboard.Key.home:       "HOME",
        keyboard.Key.end:        "END",
        keyboard.Key.page_up:    "PAGE_UP",
        keyboard.Key.page_down:  "PAGE_DOWN",
        keyboard.Key.caps_lock:  "CAPS_LOCK",
        keyboard.Key.shift:      "SHIFT",
        keyboard.Key.shift_r:    "SHIFT",
        keyboard.Key.ctrl:       "CTRL",
        keyboard.Key.ctrl_r:     "CTRL",
        keyboard.Key.alt:        "ALT",
        keyboard.Key.alt_gr:     "ALT",
        keyboard.Key.cmd:        "COMMAND",
        keyboard.Key.cmd_r:      "COMMAND",
    }

    active_instance = None

    def __init__(self, on_key_event_callback=None, mouse_hook=None):
        """
        :param on_key_event_callback: Callback triggered when a key is captured (payload_string).
        :param mouse_hook: Reference to the MouseHookManager instance.
        """
        self.on_key_event_callback = on_key_event_callback
        self._mouse_hook = mouse_hook

        # LISTENER 1: observe-only (suppress=False).  Started by start(), stopped by stop().
        self._observe_listener: keyboard.Listener | None = None

        # LISTENER 2: suppress=True.  Started/stopped by update_suppression_state().
        self._suppress_listener: keyboard.Listener | None = None

        # _observe_lock guards _observe_listener lifecycle.
        # _suppress_lock guards _suppress_listener lifecycle.
        # They are intentionally SEPARATE to avoid any cross-lock ordering deadlocks.
        self._observe_lock = threading.Lock()
        self._suppress_lock = threading.Lock()

        import atexit
        atexit.register(self._emergency_release)

        KeyboardHookManager.active_instance = self

    # ── Mouse hook linkage ───────────────────────────────────────────────────

    @property
    def mouse_hook(self):
        if self._mouse_hook is not None:
            return self._mouse_hook
        try:
            from input.mouse_hook import MouseHookManager
            return MouseHookManager.instance
        except Exception:
            return None

    @mouse_hook.setter
    def mouse_hook(self, value):
        self._mouse_hook = value

    # ── Focus state ──────────────────────────────────────────────────────────

    @property
    def is_trapped(self) -> bool:
        """True when keyboard input is being forwarded and suppressed."""
        mh = self.mouse_hook
        return bool(mh and mh.keyboard_focused_on_android)

    # ── Public lifecycle ─────────────────────────────────────────────────────

    def start(self):
        """
        Starts the always-on observe-only listener (LISTENER 1).
        Safe to call multiple times; a running listener is not restarted.
        """
        with self._observe_lock:
            if self._observe_listener is not None:
                logger.debug("Observe keyboard listener already running.")
                return
            try:
                ol = keyboard.Listener(
                    on_press=self._on_press,
                    on_release=self._on_release,
                    suppress=False      # ← NO XGrabKeyboard; zero lockup risk
                )
                ol.start()
                self._observe_listener = ol
                logger.info("Observe-only keyboard listener started.")
            except Exception as e:
                logger.error(f"Failed to start observe keyboard listener: {e}")
                self._observe_listener = None

    def stop(self):
        """Stops both listeners and releases all X11 grabs."""
        self._stop_suppress_listener()
        with self._observe_lock:
            self._stop_observe_listener_under_lock()

    def update_suppression_state(self, focused: bool):
        """
        Called by MouseHookManager when keyboard focus on Android changes.
        focused=True  → start LISTENER 2 (suppress X11 key events to stop PC leak).
        focused=False → stop  LISTENER 2 (return normal key delivery to PC apps).

        Safe to call from any thread, INCLUDING from within _on_press (observe thread).
        The suppress listener is a separate thread from the observe listener, so
        join() on it from _on_press will never deadlock.
        """
        if focused:
            self._start_suppress_listener()
        else:
            self._stop_suppress_listener()

    # ── Listener 1: observe lifecycle (under _observe_lock) ─────────────────

    def _stop_observe_listener_under_lock(self):
        """Caller must hold self._observe_lock."""
        ol = self._observe_listener
        if ol is None:
            return
        self._observe_listener = None
        try:
            ol.stop()
            if ol.is_alive():
                ol.join(timeout=2.0)
                if ol.is_alive():
                    logger.warning("Observe keyboard listener did not exit within 2s.")
        except Exception as e:
            logger.debug(f"Error stopping observe keyboard listener: {e}")
        logger.info("Observe keyboard listener stopped.")

    # ── Listener 2: suppress lifecycle ───────────────────────────────────────

    def _start_suppress_listener(self):
        """
        (Re)starts the suppress=True listener.
        Always tears down the old one first to prevent double XGrabKeyboard.
        """
        with self._suppress_lock:
            # Stop any existing suppress listener cleanly before starting a new one.
            self._stop_suppress_listener_under_lock()
            try:
                sl = keyboard.Listener(
                    on_press=self._suppress_noop,
                    on_release=self._suppress_noop,
                    suppress=True       # ← This IS the XGrabKeyboard; intentional.
                )
                sl.start()
                self._suppress_listener = sl
                logger.info("Suppress keyboard listener started (X11 keys swallowed – PC leak stopped).")
            except Exception as e:
                logger.error(f"Failed to start suppress keyboard listener: {e}")
                self._suppress_listener = None

    def _stop_suppress_listener(self):
        """Stops the suppress listener. Safe to call from ANY thread."""
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
                    logger.warning("Suppress keyboard listener did not exit within 2s.")
        except Exception as e:
            logger.debug(f"Error stopping suppress keyboard listener: {e}")
        logger.info("Suppress keyboard listener stopped. Native key delivery restored.")

    @staticmethod
    def _suppress_noop(key):
        """Suppress-listener callback: silently consume the event."""
        pass

    # ── Emergency teardown ───────────────────────────────────────────────────

    def _emergency_release(self):
        """
        atexit handler. Forces teardown of the suppress listener to release
        XGrabKeyboard even if the normal shutdown path was never reached.
        Errors are silently swallowed because logging may be torn down by the time
        atexit handlers run.
        """
        try:
            self._stop_suppress_listener()
        except Exception:
            pass

    # ── pynput observe callbacks ─────────────────────────────────────────────

    def _on_press(self, key):
        """
        Called by LISTENER 1 (suppress=False) for every keypress.

        If `keyboard_focused_on_android` is True:
          • Build the protocol payload and send it to the Android client.
          • The X11 event is simultaneously consumed by LISTENER 2 (suppress=True)
            so it never reaches the focused PC application.

        If `keyboard_focused_on_android` is False:
          • Do nothing; the key passes through to the PC normally (LISTENER 1
            does NOT suppress it, and LISTENER 2 is not running).

        There is NO deadlock risk here: this callback runs on the _observe_listener
        thread (suppress=False).  If focus is lost mid-stream and we need to stop
        the suppress listener, we call _stop_suppress_listener() which joins the
        SEPARATE _suppress_listener thread — not ourselves.
        """
        mh = self.mouse_hook
        if not (mh and mh.keyboard_focused_on_android):
            # Not focused on Android → let the key through normally (no-op here).
            # Also ensure the suppress listener is stopped in case focus was just lost.
            # This call is safe: it joins _suppress_listener, not the observe thread.
            self._stop_suppress_listener()
            return

        payload = None
        if hasattr(key, 'char') and key.char is not None:
            payload = f"K:TEXT:{key.char}"
        elif key == keyboard.Key.space:
            payload = "K:TEXT: "
        elif key in self.SPECIAL_KEY_MAP:
            payload = f"K:ACT:{self.SPECIAL_KEY_MAP[key]}"

        if payload and self.on_key_event_callback:
            try:
                self.on_key_event_callback(payload)
            except Exception as e:
                logger.error(f"Error executing key event callback: {e}")

    def _on_release(self, key):
        """Key release: no-op (release signals are not forwarded to Android)."""
        pass
