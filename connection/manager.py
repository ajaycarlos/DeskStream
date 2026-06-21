import subprocess
import logging
import threading
from .tcp_server import TCPHostServer
from .udp_sender import UDPMouseStreamer

logger = logging.getLogger("deskstream.connection.manager")

class ConnectionManager:
    """
    Orchestrates the dual connection layers (USB/ADB and Wi-Fi TCP/UDP).
    Handles ADB command execution and fail-safe recovery for USB connection states.
    """
    def __init__(self, settings_manager, on_unlock_callback=None):
        self.settings = settings_manager
        self.on_unlock_callback = on_unlock_callback
        self.tcp_server = TCPHostServer(settings_manager, on_unlock_callback=self._on_unlock_received)
        self.udp_sender = UDPMouseStreamer()
        self.is_streaming = False
        self.lock = threading.Lock()

    def _on_unlock_received(self):
        logger.info("ConnectionManager received unlock request from connection layer.")
        if self.on_unlock_callback:
            try:
                self.on_unlock_callback()
            except Exception as e:
                logger.error(f"Error processing unlock callback in ConnectionManager: {e}")

    def start_services(self):
        """Initializes TCP server listening pipelines."""
        mode = self.settings.get_connection_mode()
        devices = self.settings.get_saved_devices()
        
        # Determine port - default to 8080
        port = 8080
        if devices:
            port = devices[0].get("preferred_port", 8080)

        logger.info(f"Starting connection services in {mode} mode.")

        if mode == "USB":
            # Attempt to set up ADB port forwarding
            self._setup_adb_tunnel(port)
            # Bind TCP to localhost only for USB security
            self.tcp_server.start(port=port)
        else:
            # Bind TCP to open interfaces for Wi-Fi pairing
            self.tcp_server.start(port=port)

    def stop_services(self):
        """Stops both TCP and UDP listening/sending layers."""
        self.stop_streaming()
        self.tcp_server.stop()
        
        mode = self.settings.get_connection_mode()
        if mode == "USB":
            devices = self.settings.get_saved_devices()
            port = devices[0].get("preferred_port", 8080) if devices else 8080
            self._remove_adb_tunnel(port)

    def _setup_adb_tunnel(self, port):
        """
        Executes 'adb reverse' via subprocess to forward Android client requests
        to the PC host's TCP server. Catches errors gracefully if phone is absent.
        """
        try:
            logger.info(f"Setting up ADB reverse tunnel: adb reverse tcp:{port} tcp:{port}")
            # Run command to reverse port forwarding. If device is absent, this will return non-zero
            result = subprocess.run(
                ["adb", "reverse", f"tcp:{port}", f"tcp:{port}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True
            )
            logger.info("ADB reverse tunnel established successfully.")
        except FileNotFoundError:
            logger.error("ADB command not found on host. Please install android-tools-adb.")
        except subprocess.CalledProcessError as e:
            # Device not connected or USB debugging disabled
            stderr_cleaned = e.stderr.strip().replace("\n", " ")
            logger.warning(
                f"ADB Tunnel Configuration failed: {stderr_cleaned}. "
                "Ensure your Android device is plugged in via USB and USB Debugging is authorized."
            )
        except Exception as e:
            logger.error(f"Unexpected error configuring ADB tunnel: {e}")

    def _remove_adb_tunnel(self, port):
        """Cleans up the reversed ADB port when closing USB mode."""
        try:
            logger.info(f"Removing ADB reverse tunnel for port {port}")
            subprocess.run(
                ["adb", "reverse", "--remove", f"tcp:{port}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True
            )
        except Exception as e:
            logger.debug(f"Non-critical cleanup: ADB reverse removal failed: {e}")

    def start_streaming(self):
        """Activates mouse streaming data routing."""
        with self.lock:
            if self.is_streaming:
                return
            
            mode = self.settings.get_connection_mode()
            devices = self.settings.get_saved_devices()
            
            if mode == "WIFI":
                if not devices:
                    logger.error("Cannot start Wi-Fi streaming: No paired device configured.")
                    return
                
                device = devices[0]
                target_ip = device["last_known_ip"]
                # Mouse updates go to target_port + 1 (UDP)
                target_port = device["preferred_port"] + 1
                
                self.udp_sender.start(target_ip, target_port)
            
            self.is_streaming = True
            logger.info(f"Data streaming activated ({mode} transport).")

    def stop_streaming(self):
        """Deactivates mouse streaming data routing."""
        with self.lock:
            if not self.is_streaming:
                return
            
            self.udp_sender.stop()
            self.is_streaming = False
            logger.info("Data streaming deactivated.")

    def send_mouse_delta(self, dx, dy):
        """
        Sends coordinate deltas to client.
        WiFi uses UDP for lower latency. USB uses TCP (ADB reverse) for stable packet delivery.
        """
        if not self.is_streaming:
            return

        mode = self.settings.get_connection_mode()
        if mode == "WIFI":
            self.udp_sender.send_mouse_delta(dx, dy)
        elif mode == "USB":
            # For USB mode, we pipe mouse events directly over the reversed TCP control socket
            self.tcp_server.send_message(f"M:{dx}:{dy}")

    def send_mouse_click(self, button, state):
        """Sends mouse click events to client."""
        if not self.is_streaming:
            return

        mode = self.settings.get_connection_mode()
        if mode == "WIFI":
            self.udp_sender.send_mouse_click(button, state)
        elif mode == "USB":
            # Sanitize button naming
            btn = str(button).upper()
            btn = "LEFT" if "LEFT" in btn else ("RIGHT" if "RIGHT" in btn else ("MIDDLE" if "MIDDLE" in btn else btn))
            self.tcp_server.send_message(f"C:{btn}:{state}")
