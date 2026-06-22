import os
import sys
import time
import signal
import logging
import queue
from config.settings import SettingsManager
from connection.manager import ConnectionManager
from input.mouse_hook import MouseHookManager, get_primary_monitor_resolution
from input.keyboard_hook import KeyboardHookManager
from ui.tray import TrayIconManager
from ui.app_window import ConfigWindow

# Configure clean logging to stdout
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("deskstream.main")

class DeskStreamApp:
    """
    Main Application Bootstrap for DeskStream Sync.
    Integrates system tray icons, configuration GUI panel, and hardware input listeners.
    """
    def __init__(self):
        self.settings = SettingsManager()
        self.gui_queue = queue.Queue()
        
        # Initialize connection manager with unlock + device-info + disconnect callbacks
        self.connection = ConnectionManager(
            self.settings,
            on_unlock_callback=self._handle_unlock_request,
            on_device_info_callback=self._handle_device_info,
            on_client_disconnect_callback=self._handle_client_disconnect
        )
        
        # Initialize keyboard tracker with text/action routing callback
        self.keyboard_hook = KeyboardHookManager(
            on_key_event_callback=self._handle_key_event
        )

        # Initialize mouse tracker with callbacks for screen exit, movement deltas, clicks, scroll, and unlock
        self.mouse_hook = MouseHookManager(
            self.settings,
            on_screen_exit_callback=self._handle_screen_exit,
            on_mouse_move_delta_callback=self._handle_mouse_move,
            on_mouse_click_callback=self._handle_mouse_click,
            on_mouse_scroll_callback=self._handle_mouse_scroll,
            on_unlock_callback=self._handle_unlock_request
        )
        
        # Link keyboard hook to mouse hook for state dependency
        self.keyboard_hook.mouse_hook = self.mouse_hook

        # Initialize system tray icon manager
        self.tray = TrayIconManager(
            on_settings_clicked_callback=self._queue_settings,
            on_quit_clicked_callback=self._queue_quit
        )
        
        self.settings_window = None
        self.running = False

        # Auto-launch settings configuration window on startup
        self.gui_queue.put("SHOW_SETTINGS")

    def _handle_screen_exit(self, edge, trap_x, trap_y):
        """Callback fired when the mouse hits the configured screen boundary past the friction threshold."""
        logger.info(f"Boundary breach detected on edge: {edge}. Routing controls to Android.")

        # 1. Activate UDP/TCP coordinate data streams
        self.connection.start_streaming()

        # 2. Lock the local mouse position to trap it at the screen boundary
        self.mouse_hook.trap_cursor(trap_x, trap_y)

        # 3. Keyboard suppression is driven by click-to-focus (left-click on Android
        #    sets mouse_hook.keyboard_focused_on_android which notifies keyboard_hook).
        #    No explicit suppression call needed here; setting it would race with the
        #    click-to-focus path and potentially start the suppress listener twice.

    def _handle_device_info(self, width: int, height: int, density_dpi: int = 0):
        """Callback fired when the Android client sends its INIT resolution packet."""
        logger.info(f"Android device resolution received: {width}x{height}, DPI {density_dpi}. Updating treadmill clamps.")
        self.mouse_hook.set_android_resolution(width, height, density_dpi)

    def _handle_client_disconnect(self):
        """
        Callback fired immediately when the Android client TCP socket drops
        (graceful OR abrupt).  Delegates to mouse_hook.on_client_disconnect()
        which untraps the cursor and stops streaming if it was active.

        This fires BEFORE the next INIT handshake, closing the window where
        cursor_on_android would otherwise stay True with a live treadmill and
        stale velocity accumulators — the root cause of the reconnect speed explosion.
        """
        logger.info("TCP client disconnected. Delegating teardown to mouse hook.")
        self.mouse_hook.on_client_disconnect()

    def _handle_mouse_move(self, dx, dy):
        """Callback fired to forward trapped mouse relative coordinates."""
        self.connection.send_mouse_delta(dx, dy)

    def _handle_mouse_scroll(self, dy):
        """Callback fired to forward scroll-wheel ticks over the wire."""
        self.connection.send_mouse_scroll(dy)

    def _handle_mouse_click(self, button, state):
        """Callback fired to forward trapped mouse click states."""
        self.connection.send_mouse_click(button, state)

    def _handle_key_event(self, payload_string):
        """Callback fired to forward trapped keyboard inputs over TCP."""
        self.connection.tcp_server.send_message(payload_string)

    def _handle_unlock_request(self):
        """Callback fired when the Android client sends an UNLOCK message."""
        logger.info("Unlock command received from connection bridge. Releasing inputs.")

        # 1. Deactivate streaming
        self.connection.stop_streaming()

        # 2. Release local mouse cursor (also clears keyboard_focused_on_android,
        #    which in turn calls keyboard_hook.update_suppression_state(False))
        self.mouse_hook.untrap_cursor()

        # 3. Belt-and-suspenders: ensure suppress listener is stopped even if the
        #    mouse hook's notification path had an exception.
        self.keyboard_hook.update_suppression_state(False)

    def _queue_settings(self):
        """Pushes settings window request to GUI event queue."""
        self.gui_queue.put("SHOW_SETTINGS")

    def _queue_quit(self):
        """Pushes quit request to GUI event queue."""
        self.gui_queue.put("QUIT")

    def _on_settings_saved(self):
        """Fired when config.json changes are saved from GUI. Refreshes active system parameters."""
        logger.info("Config changed. Restarting network services and updating coordinate margins...")
        
        # Restart TCP listeners / ADB tunnel with new ports/modes
        self.connection.stop_services()
        self.connection.start_services()

        # Re-query primary monitor layout in case user adjusted edge alignment
        width, height = get_primary_monitor_resolution()
        self.mouse_hook.screen_width = width
        self.mouse_hook.screen_height = height
        logger.info(f"Resolution updated to {width}x{height}.")

    def start(self):
        """Starts the background services, tray icon, and input listeners.

        BUG 1 FIX – Startup Order Lockup:
        Input hooks MUST be started before the TCP server begins accepting
        connections.  If the Android client connects before pynput listeners
        are alive, the INIT handshake fires _handle_device_info and potentially
        triggers keyboard-suppression state changes (update_suppression_state)
        while KeyboardHookManager.listener is still None.  The resulting
        race condition leaves the X11 keyboard grab in a half-initialised state
        that manifests as an I-beam text cursor and dead keyboard/clicks.

        Correct order:
          1. Start mouse + keyboard hooks (pynput listeners now alive)
          2. Start network services (TCP server can now safely accept clients)
          3. Start system tray
        """
        self.running = True

        # 1. Start global mouse hook FIRST – pynput listeners must be alive
        #    before any TCP client can trigger input-suppression callbacks.
        self.mouse_hook.start()

        # 2. Start observe-only keyboard hook so key forwarding is live.
        #    Must be before TCP start so the INIT handshake cannot race with
        #    keyboard focus changes before the listener exists.
        self.keyboard_hook.start()

        # 3. Start network server pipelines (TCP and UDP/ADB)
        #    Safe now: pynput is fully initialised and ready.
        self.connection.start_services()

        # 4. Start System Tray icon daemon
        self.tray.start()

        logger.info("DeskStream Sync Host application fully initialized.")

    def stop(self):
        """Shuts down all threads, services, windows, and hooks cleanly."""
        if not self.running:
            return
        
        self.running = False
        logger.info("Stopping all background services...")
        
        # Stop input hooks first to release user controls immediately
        self.mouse_hook.stop()
        self.keyboard_hook.stop()
        
        # Close settings GUI if active
        if self.settings_window:
            self.settings_window.close()
            self.settings_window = None

        # Stop TCP/UDP sockets and clean up ADB tunnels
        self.connection.stop_services()
        
        logger.info("Clean shutdown completed successfully.")

def main():
    app = DeskStreamApp()

    # Capture standard termination signals for clean recovery
    def sig_handler(signum, frame):
        logger.info(f"Signal {signum} received.")
        app.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    try:
        app.start()
        
        last_connected_state = False
        
        # Main execution thread keeper loop (Updates non-blocking GUI updates)
        while app.running:
            # 1. Check TCP connection state changes to update Tray Icon indicator
            connected = app.connection.tcp_server.is_client_connected()
            if connected != last_connected_state:
                app.tray.update_connection_state(connected)
                last_connected_state = connected

            # 2. Process Thread-safe GUI requests
            try:
                task = app.gui_queue.get_nowait()
                if task == "SHOW_SETTINGS":
                    if app.settings_window is None or app.settings_window.is_closed:
                        app.settings_window = ConfigWindow(
                            app.settings,
                            on_save_callback=app._on_settings_saved,
                            mouse_hook_manager=app.mouse_hook
                        )
                    else:
                        app.settings_window.focus()
                elif task == "QUIT":
                    app.running = False
            except queue.Empty:
                pass

            # 3. Run Tkinter event loop iteration if window is active
            if app.settings_window:
                app.settings_window.update()
                if app.settings_window.is_closed:
                    app.settings_window = None

            time.sleep(0.05)
            
    except KeyboardInterrupt:
        logger.info("Keyboard Interrupt detected.")
    finally:
        app.stop()

if __name__ == "__main__":
    main()
