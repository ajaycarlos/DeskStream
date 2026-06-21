import socket
import threading
import logging
import time

logger = logging.getLogger("deskstream.connection.tcp")

class TCPHostServer:
    """
    TCP Server acting as the control channel for DeskStream Sync.
    Handles pairing handshake verification (IP pinning) and reliable keyboard event routing.
    """
    def __init__(self, settings_manager, on_unlock_callback=None):
        self.settings = settings_manager
        self.on_unlock_callback = on_unlock_callback
        self.server_sock = None
        self.client_sock = None
        self.client_addr = None
        self.listener_thread = None
        self.is_running = False
        self.lock = threading.Lock()

    def start(self, port=8080):
        """Starts the background listening thread for incoming TCP client connections."""
        with self.lock:
            if self.is_running:
                logger.warning("TCP Host Server is already running.")
                return
            
            self.is_running = True
            
        self.listener_thread = threading.Thread(target=self._listen_loop, args=(port,), daemon=True)
        self.listener_thread.start()
        logger.info(f"TCP Host Server listener thread spawned on port {port}.")

    def stop(self):
        """Stops the TCP server and closes all active sockets."""
        with self.lock:
            self.is_running = False
            
            # Close active client connection
            if self.client_sock:
                try:
                    self.client_sock.shutdown(socket.SHUT_RDWR)
                    self.client_sock.close()
                except Exception as e:
                    logger.debug(f"Error closing client socket: {e}")
                self.client_sock = None
                self.client_addr = None

            # Close main server socket
            if self.server_sock:
                try:
                    self.server_sock.close()
                except Exception as e:
                    logger.debug(f"Error closing server socket: {e}")
                self.server_sock = None

        logger.info("TCP Host Server stopped.")

    def _verify_client(self, client_ip):
        """
        Enforces strict cybersecurity validation (IP pinning) on incoming client connections.
        Prevents unauthorized network sniffing or command injection.
        """
        mode = self.settings.get_connection_mode()

        if mode == "USB":
            # USB mode tunnels through ADB, client must originate from loopback
            if client_ip in ("127.0.0.1", "::1", "localhost"):
                return True
            logger.warning(f"Security Alert: Blocked non-loopback connection {client_ip} in USB mode.")
            return False

        elif mode == "WIFI":
            # Wi-Fi mode must match one of the whitelisted/paired IP addresses
            saved_devices = self.settings.get_saved_devices()
            allowed_ips = {dev["last_known_ip"] for dev in saved_devices}
            
            if client_ip in allowed_ips:
                return True
            
            logger.warning(f"Security Alert: Blocked unauthorized connection attempt from IP: {client_ip}")
            return False

        return False

    def _listen_loop(self, port):
        """Background thread worker that binds to socket and accepts incoming connections."""
        mode = self.settings.get_connection_mode()
        
        # Bind to localhost only in USB mode for enhanced security
        bind_ip = "127.0.0.1" if mode == "USB" else "0.0.0.0"

        try:
            self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server_sock.bind((bind_ip, port))
            self.server_sock.listen(1)
            logger.info(f"TCP server bound to {bind_ip}:{port}. Awaiting client handshakes...")
        except Exception as e:
            logger.error(f"Failed to bind TCP server to port {port}: {e}")
            with self.lock:
                self.is_running = False
            return

        while True:
            with self.lock:
                if not self.is_running:
                    break

            try:
                # Set a timeout so the loop checks the is_running flag regularly
                self.server_sock.settimeout(2.0)
                client_sock, client_addr = self.server_sock.accept()
            except socket.timeout:
                continue
            except Exception as e:
                with self.lock:
                    if self.is_running:
                        logger.error(f"Error accepting connection: {e}")
                break

            client_ip = client_addr[0]
            logger.info(f"Incoming connection attempt from {client_ip}:{client_addr[1]}")

            if self._verify_client(client_ip):
                with self.lock:
                    # Close previous connection if any
                    if self.client_sock:
                        try:
                            self.client_sock.close()
                        except Exception:
                            pass
                    
                    self.client_sock = client_sock
                    self.client_addr = client_addr
                    logger.info(f"Client {client_ip} verified and connected successfully.")
                
                # Start client receiver thread for heartbeat / incoming control messages
                threading.Thread(target=self._handle_client, args=(client_sock, client_addr), daemon=True).start()
            else:
                try:
                    client_sock.close()
                except Exception:
                    pass

    def _handle_client(self, client_sock, client_addr):
        """Receives incoming data (heartbeats or exit notifications) from the client."""
        client_ip = client_addr[0]
        try:
            client_sock.settimeout(10.0) # Heartbeat timeout
            while True:
                data = client_sock.recv(1024)
                if not data:
                    logger.info(f"Client {client_ip} disconnected gracefully.")
                    break
                
                # Process incoming commands (e.g. UNLOCK request when client exits Android screen edge)
                msg = data.decode("utf-8").strip()
                if msg:
                    logger.debug(f"Received from client: {msg}")
                    if msg == "UNLOCK" or msg.startswith("UNLOCK"):
                        logger.info("Received UNLOCK command from Android client. Unlocking cursor.")
                        if self.on_unlock_callback:
                            try:
                                self.on_unlock_callback()
                            except Exception as e:
                                logger.error(f"Error executing on_unlock_callback: {e}")
                    
        except socket.timeout:
            logger.warning(f"Connection to client {client_ip} timed out.")
        except Exception as e:
            logger.debug(f"Exception handling client {client_ip}: {e}")
        finally:
            with self.lock:
                if self.client_sock == client_sock:
                    self.client_sock = None
                    self.client_addr = None
            try:
                client_sock.close()
            except Exception:
                pass

    def send_message(self, message: str) -> bool:
        """Sends a text message to the connected client. Thread-safe."""
        # Append newline so the client parser can separate messages easily
        payload = (message + "\n").encode("utf-8")
        
        with self.lock:
            sock = self.client_sock
            
        if not sock:
            logger.debug("No client connected. Message dropped.")
            return False

        try:
            sock.sendall(payload)
            return True
        except Exception as e:
            logger.warning(f"Failed to send TCP message, client disconnected: {e}")
            with self.lock:
                if self.client_sock == sock:
                    self.client_sock = None
                    self.client_addr = None
            try:
                sock.close()
            except Exception:
                pass
            return False

    def send_keyboard_text(self, text: str) -> bool:
        """Formats and sends keyboard character payload."""
        return self.send_message(f"K:TEXT:{text}")

    def send_keyboard_action(self, key_code: str) -> bool:
        """Formats and sends special action keyboard keys (e.g., BACKSPACE)."""
        return self.send_message(f"K:ACT:{key_code}")

    def is_client_connected(self) -> bool:
        """Returns True if there is a verified connected client."""
        with self.lock:
            return self.client_sock is not None
