import logging
import threading
import time
import subprocess
import re
from pynput import mouse

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
    Manages global mouse hooks, edge checking, edge friction, and cursor trapping.
    """
    def __init__(self, settings_manager, on_screen_exit_callback=None, on_mouse_move_delta_callback=None, on_mouse_click_callback=None):
        """
        :param settings_manager: Instance of SettingsManager.
        :param on_screen_exit_callback: Function to invoke when cursor exits screen (e.g. on_screen_exit(edge, trap_x, trap_y)).
        :param on_mouse_move_delta_callback: Function to invoke when cursor is trapped and moves (e.g. on_move_delta(dx, dy)).
        :param on_mouse_click_callback: Function to invoke when click is detected while trapped (e.g. on_click(button, state)).
        """
        self.settings = settings_manager
        self.on_screen_exit_callback = on_screen_exit_callback
        self.on_mouse_move_delta_callback = on_mouse_move_delta_callback
        self.on_mouse_click_callback = on_mouse_click_callback

        self.mouse_controller = mouse.Controller()
        self.listener = None
        self.state_lock = threading.Lock()

        # Monitor layout dimensions
        self.screen_width, self.screen_height = get_primary_monitor_resolution()

        # Trapping state parameters
        self.is_trapped = False
        self.trap_x = 0
        self.trap_y = 0
        self._setting_position = False

        # Edge friction timing state variables
        self.at_edge = False
        self.friction_timer = None

    def start(self):
        """Starts the global mouse listener thread."""
        with self.state_lock:
            if self.listener is not None:
                logger.warning("Mouse listener thread is already active.")
                return

            self.listener = mouse.Listener(
                on_move=self._on_move,
                on_click=self._on_click,
                on_scroll=self._on_scroll
            )
            self.listener.start()
            logger.info("Global mouse tracking listener thread started.")

    def stop(self):
        """Stops the global mouse listener thread and cleans up active timers."""
        with self.state_lock:
            self._cancel_friction_timer_under_lock()
            self.is_trapped = False
            if self.listener:
                self.listener.stop()
                self.listener = None
            logger.info("Global mouse tracking listener thread stopped.")

    def trap_cursor(self, x, y):
        """
        Locks the physical mouse cursor at the screen boundary.
        """
        with self.state_lock:
            self.trap_x = x
            self.trap_y = y
            self.is_trapped = True
            
            # Snap mouse immediately to target coordinates
            self._setting_position = True
            self.mouse_controller.position = (x, y)
            logger.info(f"Cursor locked and trapped at boundary coordinate: ({x}, {y})")

    def untrap_cursor(self):
        """Unlocks the physical mouse cursor, returning normal movement to the user."""
        with self.state_lock:
            self.is_trapped = False
            logger.info("Cursor untrapped. Local control restored.")

    def _cancel_friction_timer_under_lock(self):
        """Cancels any running edge friction timers."""
        if self.friction_timer:
            self.friction_timer.cancel()
            self.friction_timer = None
        self.at_edge = False

    def _on_friction_timer_fired(self, edge, x, y):
        """Invoked when the user has held the cursor against the edge for the required friction duration."""
        with self.state_lock:
            # Re-verify that we are still at the edge and not trapped
            if not self.at_edge or self.is_trapped:
                return
            
            logger.info(f"Friction boundary threshold crossed on the {edge} edge.")
            self._cancel_friction_timer_under_lock()

        # Determine the exact locking coordinate on the edge
        trap_x, trap_y = x, y
        if edge == "RIGHT":
            trap_x = self.screen_width - 1
        elif edge == "LEFT":
            trap_x = 0
        elif edge == "TOP":
            trap_y = 0
        elif edge == "BOTTOM":
            trap_y = self.screen_height - 1

        # Fire callback to alert manager/connection layer
        if self.on_screen_exit_callback:
            try:
                self.on_screen_exit_callback(edge, trap_x, trap_y)
            except Exception as e:
                logger.error(f"Error executing on_screen_exit callback: {e}")

    def _check_edge(self, x, y):
        """Checks if the coordinate matches the selected edge boundary."""
        edge = self.settings.get_selected_edge()
        is_hit = False

        if edge == "RIGHT" and x >= self.screen_width - 1:
            is_hit = True
        elif edge == "LEFT" and x <= 0:
            is_hit = True
        elif edge == "TOP" and y <= 0:
            is_hit = True
        elif edge == "BOTTOM" and y >= self.screen_height - 1:
            is_hit = True

        return is_hit, edge

    def _on_move(self, x, y):
        """Global mouse move event callback."""
        # Prevent tracking loops caused by snapping cursor back
        if self._setting_position:
            self._setting_position = False
            return

        # Check if cursor is trapped
        with self.state_lock:
            is_trapped = self.is_trapped
            trap_x = self.trap_x
            trap_y = self.trap_y

        if is_trapped:
            # Calculate delta movement from lock origin
            dx = x - trap_x
            dy = y - trap_y

            if dx != 0 or dy != 0:
                # Forward coordinates delta
                if self.on_mouse_move_delta_callback:
                    try:
                        self.on_mouse_move_delta_callback(dx, dy)
                    except Exception as e:
                        logger.error(f"Error in mouse move delta callback: {e}")

                # Force cursor back to trap point to maintain confinement
                self._setting_position = True
                self.mouse_controller.position = (trap_x, trap_y)
            return

        # Regular boundary checking when not trapped
        is_hit, edge = self._check_edge(x, y)

        if is_hit:
            with self.state_lock:
                if not self.at_edge:
                    self.at_edge = True
                    friction_ms = self.settings.get_edge_friction_ms()
                    logger.debug(f"Mouse reached boundary ({edge} edge). Starting friction timer for {friction_ms}ms.")
                    
                    # Start asynchronous timer for friction delay
                    self.friction_timer = threading.Timer(
                        friction_ms / 1000.0,
                        self._on_friction_timer_fired,
                        args=[edge, x, y]
                    )
                    self.friction_timer.start()
        else:
            with self.state_lock:
                if self.at_edge:
                    logger.debug(f"Mouse moved away from ({edge} edge). Canceling friction timer.")
                    self._cancel_friction_timer_under_lock()

    def _on_click(self, x, y, button, pressed):
        """Global mouse click event callback."""
        with self.state_lock:
            is_trapped = self.is_trapped
        
        if is_trapped:
            state = 1 if pressed else 0
            if self.on_mouse_click_callback:
                try:
                    self.on_mouse_click_callback(button, state)
                except Exception as e:
                    logger.error(f"Error executing on_mouse_click callback: {e}")
            logger.debug(f"Click events captured while trapped: Button={button}, Pressed={pressed}")

    def _on_scroll(self, x, y, dx, dy):
        """Global mouse scroll event callback."""
        with self.state_lock:
            is_trapped = self.is_trapped
            
        if is_trapped:
            # Scroll movements while locked will be dispatched to Android client in future steps
            logger.debug(f"Scroll events captured while trapped: dx={dx}, dy={dy}")
