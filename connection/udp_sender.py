import socket
import logging

logger = logging.getLogger("deskstream.connection.udp")

class UDPMouseStreamer:
    """
    Manages low-latency UDP streaming of mouse events (movement deltas and clicks)
    to the target Android client.
    """
    def __init__(self):
        self.sock = None
        self.target_ip = None
        self.target_port = None

    def start(self, target_ip, target_port):
        """Initializes the UDP socket and sets target destination."""
        self.target_ip = target_ip
        self.target_port = target_port
        
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            # Optimize socket buffer size for real-time streaming
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
            logger.info(f"UDP Streamer initialized for destination {self.target_ip}:{self.target_port}")
        except Exception as e:
            logger.error(f"Failed to initialize UDP socket: {e}")
            self.sock = None

    def stop(self):
        """Closes the UDP socket securely."""
        if self.sock:
            try:
                self.sock.close()
            except Exception as e:
                logger.error(f"Error closing UDP socket: {e}")
            self.sock = None
        logger.info("UDP Streamer stopped.")

    def _send(self, payload: str):
        """Internal helper to transmit payload with strict exception containment."""
        if not self.sock:
            logger.warning("UDP socket is not initialized. Dropping payload.")
            return

        try:
            self.sock.sendto(payload.encode("utf-8"), (self.target_ip, self.target_port))
        except OSError as e:
            # Handle standard network issues (unreachable host, route down) silently/resiliently
            logger.debug(f"Network transient error sending UDP packet: {e}")
        except Exception as e:
            logger.error(f"Unexpected error in UDP send: {e}")

    def send_mouse_delta(self, dx: int, dy: int):
        """
        Sends relative mouse movement coordinates.
        Payload layout: M:[dX]:[dY]
        """
        payload = f"M:{dx}:{dy}"
        self._send(payload)

    def send_mouse_click(self, button: str, state: int):
        """
        Sends click actions.
        Payload layout: C:[BUTTON]:[STATE] (STATE: 1=Press, 0=Release)
        """
        # Sanitize button value
        btn_str = str(button).upper()
        if "LEFT" in btn_str:
            btn = "LEFT"
        elif "RIGHT" in btn_str:
            btn = "RIGHT"
        elif "MIDDLE" in btn_str:
            btn = "MIDDLE"
        else:
            btn = btn_str

        payload = f"C:{btn}:{state}"
        self._send(payload)
