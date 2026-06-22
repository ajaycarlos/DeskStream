import socket
import threading
import logging
import time

logger = logging.getLogger("deskstream.connection.tcp")

class TCPHostServer:
    """
    TCP Server acting as the control channel for DeskStream Sync.
    Handles pairing handshake verification (IP pinning) and reliable keyboard event routing.

    FIX – Bug 1 (Save & Apply crash loop):
    The original stop() set is_running=False but returned immediately without
    joining the listener thread.  The listener thread was blocked on
    server_sock.accept() with a 2s timeout, meaning it could still be alive
    (and still holding the bound socket) when start() was called again.
    Even though SO_REUSEADDR is set, the OS may not release the port until
    the old listener thread actually closes its socket.  start() then saw
    is_running=True (stale from the previous cycle) and returned early with
    a "already running" warning, so the new server was never bound.
    The Android client kept hitting the dead old socket → CONNECTING, then
    the old thread finally died → DISCONNECTED, then the new one came up →
    CONNECTED. That produced the rapid flicker.

    Fix: stop() now joins the listener thread with a 5-second timeout before
    returning, ensuring the OS port is fully released.  It also resets
    is_running=False under the lock before joining so the thread exits its
    accept() loop cleanly.
    """
    def __init__(self, settings_manager, on_unlock_callback=None,
                 on_device_info_callback=None):
        self.settings = settings_manager
        self.on_unlock_callback = on_unlock_callback
        # Called with (width: int, height: int) when Android sends INIT packet
        self.on_device_info_callback = on_device_info_callback
        self.server_sock = None
        self.client_sock = None
        self.client_addr = None
        self.listener_thread = None
        self.heartbeat_thread = None   # sends PING every 5 s while a client is connected
        self.is_running = False
        self.lock = threading.Lock()

    def start(self, port=8080):
        """Starts the background listening thread for incoming TCP client connections."""
        with self.lock:
            if self.is_running:
                logger.warning("TCP Host Server is already running.")
                return
            self.is_running = True

        self.listener_thread = threading.Thread(
            target=self._listen_loop, args=(port,), daemon=True
        )
        self.listener_thread.start()
        logger.info(f"TCP Host Server listener thread spawned on port {port}.")

    def stop(self):
        """
        Stops the TCP server and closes all active sockets.
        Blocks until the listener thread has fully exited so that the OS port
        is guaranteed to be available for the next start() call.
        """
        thread_to_join = None

        with self.lock:
            if not self.is_running:
                return
            # Mark stopped FIRST so the listener loop exits on next iteration
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

            # Stop the heartbeat daemon thread
            if self.heartbeat_thread and self.heartbeat_thread.is_alive():
                self.heartbeat_thread = None  # thread reads is_running; already False above

            # Close main server socket – this unblocks the accept() call
            if self.server_sock:
                try:
                    self.server_sock.close()
                except Exception as e:
                    logger.debug(f"Error closing server socket: {e}")
                self.server_sock = None

            thread_to_join = self.listener_thread
            self.listener_thread = None

        # ── FIX: join outside the lock to avoid deadlock ─────────────────────
        # The listener thread acquires self.lock inside its loop.  If we held
        # the lock while joining we would deadlock.
        if thread_to_join and thread_to_join.is_alive():
            thread_to_join.join(timeout=5.0)
            if thread_to_join.is_alive():
                logger.warning("TCP listener thread did not exit within 5s.")

        logger.info("TCP Host Server fully stopped.")

    def _verify_client(self, client_ip):
        """
        Enforces strict cybersecurity validation (IP pinning) on incoming connections.
        Prevents unauthorized network sniffing or command injection.
        """
        mode = self.settings.get_connection_mode()

        if mode == "USB":
            if client_ip in ("127.0.0.1", "::1", "localhost"):
                return True
            logger.warning(f"Security Alert: Blocked non-loopback connection {client_ip} in USB mode.")
            return False

        elif mode == "WIFI":
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
        bind_ip = "127.0.0.1" if mode == "USB" else "0.0.0.0"

        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind((bind_ip, port))
            srv.listen(1)
            with self.lock:
                self.server_sock = srv
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
                srv.settimeout(2.0)
                client_sock, client_addr = srv.accept()
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
                    if self.client_sock:
                        try:
                            self.client_sock.close()
                        except Exception:
                            pass
                    self.client_sock = client_sock
                    self.client_addr = client_addr
                    logger.info(f"Client {client_ip} verified and connected.")

                threading.Thread(
                    target=self._handle_client,
                    args=(client_sock, client_addr),
                    daemon=True
                ).start()

                # ── Heartbeat: start / restart the PING sender daemon ─────────────
                hb = threading.Thread(
                    target=self._heartbeat_loop,
                    args=(client_sock,),
                    daemon=True,
                    name="deskstream-heartbeat"
                )
                hb.start()
                with self.lock:
                    self.heartbeat_thread = hb
            else:
                try:
                    client_sock.close()
                except Exception:
                    pass

        # Ensure server socket is closed when loop exits
        with self.lock:
            if self.server_sock:
                try:
                    self.server_sock.close()
                except Exception:
                    pass
                self.server_sock = None

    def _heartbeat_loop(self, client_sock):
        """
        Sends a lightweight 'PING\\n' packet to the connected Android client every
        5 seconds.  This prevents the Android socket's read timeout from firing
        during user-idle periods (e.g. no mouse/keyboard activity).

        The thread exits as soon as:
        - The server stops (is_running becomes False), or
        - The provided client_sock is no longer the active session socket
          (meaning the client disconnected and a new session may have started).
        """
        PING_INTERVAL = 5.0  # seconds between PING packets
        logger.debug("Heartbeat loop started.")
        while True:
            # Sleep in short increments so stop() wakes us quickly
            for _ in range(int(PING_INTERVAL / 0.5)):
                time.sleep(0.5)
                with self.lock:
                    if not self.is_running or self.client_sock is not client_sock:
                        logger.debug("Heartbeat loop exiting.")
                        return

            with self.lock:
                sock = self.client_sock if self.client_sock is client_sock else None

            if sock is None:
                logger.debug("Heartbeat loop exiting: client changed.")
                return

            try:
                sock.sendall(b"PING\n")
                logger.debug("Heartbeat PING sent.")
            except Exception as e:
                logger.debug(f"Heartbeat send failed (client likely gone): {e}")
                return

    def _handle_client(self, client_sock, client_addr):
        """Receives incoming data from the client (INIT handshake, UNLOCK, PONG heartbeats)."""
        client_ip = client_addr[0]
        buf = ""
        try:
            client_sock.settimeout(30.0)
            while True:
                chunk = client_sock.recv(1024)
                if not chunk:
                    logger.info(f"Client {client_ip} disconnected gracefully.")
                    break

                # ── FIX: accumulate bytes and split on newline ────────────────
                # recv() is a stream primitive – a single recv() call may return
                # multiple newline-delimited messages or a partial message.
                # Buffer and split properly to avoid dropping or garbling packets.
                buf += chunk.decode("utf-8", errors="replace")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    msg = line.strip()
                    if msg:
                        self._dispatch_client_message(msg, client_ip)

        except socket.timeout:
            logger.warning(f"Connection to client {client_ip} timed out (30s heartbeat).")
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

    def _dispatch_client_message(self, msg: str, client_ip: str):
        """Routes a single decoded message from the Android client."""
        logger.debug(f"Received from {client_ip}: {msg}")

        if msg.startswith("UNLOCK"):
            logger.info("Received UNLOCK command from Android client. Unlocking cursor.")
            if self.on_unlock_callback:
                try:
                    self.on_unlock_callback()
                except Exception as e:
                    logger.error(f"Error executing on_unlock_callback: {e}")

        elif msg.startswith("INIT:"):
            # ── Bug 2 – INIT handshake: "INIT:width:height:densityDpi" ──────────
            # Android sends its real screen resolution and density DPI upon TCP
            # connect so the Python host can clamp coordinates and scale sensitivity.
            try:
                parts = msg.split(":")
                if len(parts) >= 4:
                    w = int(parts[1])
                    h = int(parts[2])
                    dpi = int(parts[3])
                    logger.info(f"INIT handshake received: device resolution {w}x{h}, DPI {dpi}")
                    if self.on_device_info_callback:
                        self.on_device_info_callback(w, h, dpi)
                elif len(parts) == 3:
                    w = int(parts[1])
                    h = int(parts[2])
                    logger.info(f"INIT handshake received (legacy): device resolution {w}x{h}")
                    if self.on_device_info_callback:
                        self.on_device_info_callback(w, h, 0)
            except Exception as e:
                logger.error(f"Failed to parse INIT packet '{msg}': {e}")

        elif msg == "PONG":
            # Heartbeat round-trip reply from Android; consume silently.
            logger.debug(f"Heartbeat PONG received from {client_ip}.")

        else:
            logger.debug(f"Unknown message from client: {msg}")

    def send_message(self, message: str) -> bool:
        """Sends a text message to the connected client. Thread-safe."""
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
