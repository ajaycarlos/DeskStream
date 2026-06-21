import logging
import threading
from pynput import keyboard

logger = logging.getLogger("deskstream.keyboard_hook")

class KeyboardHookManager:
    """
    Manages global keyboard capturing.
    Conditionally suppresses keyboard events and redirects them to the Android device
    only when the cursor is in the trapped state.
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

    def __init__(self, on_key_event_callback=None):
        """
        :param on_key_event_callback: Callback triggered when a key is formatted (e.g. callback(payload_string)).
        """
        self.on_key_event_callback = on_key_event_callback
        self.listener = None
        self._is_trapped = False
        self.lock = threading.Lock()

    @property
    def is_trapped(self) -> bool:
        """Returns True if the keyboard listener is currently trapping and suppressing input."""
        with self.lock:
            return self._is_trapped

    @is_trapped.setter
    def is_trapped(self, value: bool):
        """Updates the trapping state and starts/stops the keyboard hook listener accordingly."""
        with self.lock:
            if self._is_trapped == value:
                return
            
            self._is_trapped = value
            if self._is_trapped:
                self._start_listener()
            else:
                self._stop_listener()

    def _start_listener(self):
        """Starts keyboard event suppression and listener."""
        self._stop_listener()
        self.listener = keyboard.Listener(
            on_press=self._on_press,
            on_release=self._on_release,
            suppress=True
        )
        self.listener.start()
        logger.info("Keyboard listener started in suppression mode.")

    def _stop_listener(self):
        """Stops the keyboard event listener, returning control to host OS."""
        if self.listener:
            try:
                self.listener.stop()
            except Exception as e:
                logger.debug(f"Error stopping keyboard listener: {e}")
            self.listener = None
            logger.info("Keyboard listener stopped. Native input restored.")

    def _on_press(self, key):
        """Processes key presses, formats, and forwards them to callback."""
        if not self.is_trapped:
            return

        payload = None

        # Check if it's a character key
        if hasattr(key, 'char') and key.char is not None:
            # Map character presses
            payload = f"K:TEXT:{key.char}"
        elif key == keyboard.Key.space:
            # Send space as standard character string
            payload = "K:TEXT: "
        elif key in self.SPECIAL_KEY_MAP:
            # Map special keys to actions
            payload = f"K:ACT:{self.SPECIAL_KEY_MAP[key]}"
        
        if payload and self.on_key_event_callback:
            try:
                self.on_key_event_callback(payload)
            except Exception as e:
                logger.error(f"Error executing key event callback: {e}")

    def _on_release(self, key):
        """Invoked on key release. Handled silently as release signals are ignored in standard IME typing."""
        pass

    def stop(self):
        """Cleans up the keyboard hook."""
        self.is_trapped = False
