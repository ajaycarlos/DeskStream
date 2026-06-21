import logging
import threading
from pynput import keyboard

logger = logging.getLogger("deskstream.keyboard_hook")


class KeyboardHookManager:
    """
    Manages global keyboard capturing.

    FIX NOTES (catastrophic lockup prevention):
    - pynput's keyboard.Listener with suppress=True performs an XGrabKeyboard()
      call that seizes ALL key events at the X server level.  If the listener
      thread dies unexpectedly (unhandled exception, crash, etc.) while the grab
      is active, the grab is never released.  The keyboard then appears completely
      dead to every application including the window manager, and recovery
      requires a hard reboot.
    - The fix is:
        1. Never start a new suppressed listener while one is already running –
           _stop_listener() must fully join/wait the old one first.
        2. Register an atexit handler that unconditionally releases suppression
           even if the main process exits abnormally.
        3. In _on_press / _on_release check the focus flag and return early
           (effectively a no-op) if focus was lost mid-press.
    - We still keep suppress=True because without it the keystrokes would echo
      into whatever is focused on the PC host.  But we guard every path that
      could leave the grab dangling.
    """

    SPECIAL_KEY_MAP = {
        keyboard.Key.backspace: "BACKSPACE",
        keyboard.Key.enter: "ENTER",
        keyboard.Key.tab: "TAB",
        keyboard.Key.esc: "ESCAPE",
        keyboard.Key.left: "LEFT",
        keyboard.Key.right: "RIGHT",
        keyboard.Key.up: "UP",
        keyboard.Key.down: "DOWN",
        keyboard.Key.delete: "DELETE",
        keyboard.Key.home: "HOME",
        keyboard.Key.end: "END",
        keyboard.Key.page_up: "PAGE_UP",
        keyboard.Key.page_down: "PAGE_DOWN",
        keyboard.Key.caps_lock: "CAPS_LOCK",
        keyboard.Key.shift: "SHIFT",
        keyboard.Key.shift_r: "SHIFT",
        keyboard.Key.ctrl: "CTRL",
        keyboard.Key.ctrl_r: "CTRL",
        keyboard.Key.alt: "ALT",
        keyboard.Key.alt_gr: "ALT",
        keyboard.Key.cmd: "COMMAND",
        keyboard.Key.cmd_r: "COMMAND",
    }

    active_instance = None

    def __init__(self, on_key_event_callback=None, mouse_hook=None):
        """
        :param on_key_event_callback: Callback triggered when a key is captured (payload_string).
        :param mouse_hook: Reference to the MouseHookManager instance.
        """
        self.on_key_event_callback = on_key_event_callback
        self._mouse_hook = mouse_hook
        self.listener = None
        self.lock = threading.Lock()

        # ── FIX: atexit safety net ────────────────────────────────────────────
        # If Python exits for any reason (exception, signal, etc.) while
        # suppress=True is active, the atexit handler forces listener teardown
        # before the interpreter shuts down, releasing the X11 grab.
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
        """True when keyboard input is being captured and suppressed."""
        mh = self.mouse_hook
        if mh:
            return mh.keyboard_focused_on_android
        return False

    @is_trapped.setter
    def is_trapped(self, value: bool):
        # Click-to-focus owns the state; this setter is kept for compat only.
        logger.info(f"is_trapped property set to {value} (no-op in click-to-focus mode)")

    def update_suppression_state(self, focused: bool):
        """
        Starts (focused=True) or stops (focused=False) keyboard suppression.
        Thread-safe: can be called from any thread.
        """
        with self.lock:
            if focused:
                self._start_listener_under_lock()
            else:
                self._stop_listener_under_lock()

    # ── Listener lifecycle (must be called under self.lock) ──────────────────

    def _start_listener_under_lock(self):
        """(Re)starts keyboard suppression. Caller must hold self.lock."""
        # ── FIX: always tear down the existing listener cleanly first ─────────
        # Starting a second suppressed listener without stopping the first
        # results in two concurrent XGrabKeyboard calls, undefined behaviour,
        # and a grab that can never be fully released.
        self._stop_listener_under_lock()

        try:
            self.listener = keyboard.Listener(
                on_press=self._on_press,
                on_release=self._on_release,
                suppress=True          # suppress=True IS correct here for keyboard
            )
            self.listener.start()
            logger.info("Keyboard listener started in suppression mode.")
        except Exception as e:
            # If we cannot start the listener, make absolutely sure no partial
            # grab was left dangling.
            logger.error(f"Failed to start keyboard listener: {e}")
            self.listener = None

    def _stop_listener_under_lock(self):
        """Stops the keyboard listener. Caller must hold self.lock."""
        if self.listener:
            try:
                self.listener.stop()
                # ── FIX: join the listener thread before proceeding ───────────
                # pynput's Listener.stop() signals the thread to exit, but the
                # XReleaseKeyboard call happens asynchronously inside that thread.
                # Joining ensures the X11 grab is fully released before we
                # proceed (e.g. before starting a new listener or returning to
                # the caller who may immediately assume native input is restored).
                if self.listener.is_alive():
                    self.listener.join(timeout=2.0)
                    if self.listener.is_alive():
                        logger.warning("Keyboard listener thread did not exit cleanly within 2s.")
            except Exception as e:
                logger.debug(f"Error stopping keyboard listener: {e}")
            finally:
                self.listener = None
            logger.info("Keyboard listener stopped. Native input restored.")

    # ── Emergency teardown ───────────────────────────────────────────────────

    def _emergency_release(self):
        """
        Called by atexit.  Forces listener teardown to release XGrabKeyboard
        even if the normal shutdown path was never reached (crash, signal, etc.).
        """
        try:
            with self.lock:
                self._stop_listener_under_lock()
        except Exception:
            pass  # Can't log reliably in atexit; just swallow silently.

    # ── pynput callbacks ─────────────────────────────────────────────────────

    def _on_press(self, key):
        """Processes and forwards key-press events to the Android client."""
        # ── FIX: double-check focus inside the callback ───────────────────────
        # The focus state can change between the moment the listener was started
        # and the moment a key fires (e.g. network disconnect clears focus).
        # If focus is already gone, treat the key as passthrough: returning
        # False from a suppressed listener tells pynput to propagate the event
        # to the OS instead of eating it.  However, pynput's suppress mode
        # doesn't actually support per-key passthrough on Linux/X11 – the
        # XGrabKeyboard has already consumed it.  The correct fix is to stop
        # the listener (which releases the grab) rather than trying to
        # re-inject a suppressed key.  We therefore just log and return.
        mh = self.mouse_hook
        if not (mh and mh.keyboard_focused_on_android):
            # Focus was lost; stop the listener to release the grab ASAP.
            # Use a daemon thread so we don't deadlock (can't call stop from
            # inside the listener's own thread).
            threading.Thread(
                target=self.update_suppression_state,
                args=(False,),
                daemon=True
            ).start()
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
        """Key release callback. Currently a no-op; release signals are not forwarded."""
        pass

    # ── Public cleanup ───────────────────────────────────────────────────────

    def stop(self):
        """Cleans up the keyboard hook and releases all X11 grabs."""
        with self.lock:
            self._stop_listener_under_lock()
