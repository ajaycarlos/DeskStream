import logging
import threading
from PIL import Image, ImageDraw
import pystray

logger = logging.getLogger("deskstream.ui.tray")

def create_circle_image(color_name: str) -> Image:
    """Generates a 64x64 RGBA image with a colored circle using PIL."""
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    
    # Draw simple colored indicator circle
    draw.ellipse((8, 8, 56, 56), fill=color_name)
    return image

class TrayIconManager:
    """
    Manages the desktop system tray icon, menu actions, and connection status readouts.
    Runs its event loop in a background thread to prevent blocking pynput listeners.
    """
    def __init__(self, on_settings_clicked_callback, on_quit_clicked_callback):
        self.on_settings_clicked = on_settings_clicked_callback
        self.on_quit_clicked = on_quit_clicked_callback
        
        self.connected = False
        self.status_text = "Status: Disconnected"
        self.icon = None
        self.thread = None

    def start(self):
        """Initializes and starts the system tray icon in a separate daemon thread."""
        logger.info("Initializing system tray icon...")
        
        # Define menu items with dynamic status text function
        menu = pystray.Menu(
            pystray.MenuItem(lambda text: self.status_text, lambda: None, enabled=False),
            pystray.MenuItem("Settings", self._on_settings),
            pystray.MenuItem("Quit", self._on_quit)
        )
        
        # Create default red icon (Disconnected)
        initial_image = create_circle_image("red")
        
        self.icon = pystray.Icon(
            name="DeskStream Sync",
            icon=initial_image,
            title="DeskStream Sync",
            menu=menu
        )

        # Run icon event loop in a background thread
        self.thread = threading.Thread(target=self.icon.run, daemon=True)
        self.thread.start()
        logger.info("System tray icon event thread started.")

    def update_connection_state(self, connected: bool):
        """Updates the tray icon color and status description text dynamically."""
        self.connected = connected
        if connected:
            self.status_text = "Status: Connected"
            color = "green"
        else:
            self.status_text = "Status: Disconnected"
            color = "red"
            
        if self.icon:
            # Set the new PIL image on the icon property
            self.icon.icon = create_circle_image(color)
            # Force update of the title if supported by platform
            self.icon.title = f"DeskStream Sync ({self.status_text})"
            logger.info(f"Tray status updated: {self.status_text}")

    def _on_settings(self, icon, item):
        """Settings menu item click handler."""
        logger.info("Settings selected from system tray.")
        if self.on_settings_clicked:
            self.on_settings_clicked()

    def _on_quit(self, icon, item):
        """Quit menu item click handler."""
        logger.info("Quit selected from system tray. Initiating shutdown.")
        if self.on_quit_clicked:
            self.on_quit_clicked()
        
        # Stop pystray loop
        if self.icon:
            self.icon.stop()
