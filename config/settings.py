import os
import json
import threading
import ipaddress
import logging
import copy

logger = logging.getLogger("deskstream.settings")

class SettingsManager:
    """
    Manages local application configuration with thread-safe operations.
    Enforces security boundaries by validating IP addresses and file access permissions.
    """
    DEFAULT_CONFIG = {
        "selected_edge": "RIGHT",
        "edge_friction_ms": 150,
        "connection_mode": "WIFI",
        "saved_devices": [
            {
                "device_name": "Android Mobile Device",
                "last_known_ip": "127.0.0.1",
                "preferred_port": 8080
            }
        ]
    }

    def __init__(self, config_path="config.json"):
        self.config_path = os.path.abspath(config_path)
        self.lock = threading.Lock()
        self.config = {}
        self.load()

    def load(self):
        """Loads and parses config.json. Falls back to defaults securely on failure."""
        with self.lock:
            if not os.path.exists(self.config_path):
                logger.info(f"Config file not found. Initializing with defaults at {self.config_path}")
                self.config = copy.deepcopy(self.DEFAULT_CONFIG)
                self._save_under_lock()
                return

            try:
                # Restrict permissions of existing config file to owner-only for security
                try:
                    os.chmod(self.config_path, 0o600)
                except Exception as e:
                    logger.warning(f"Failed to secure file permissions for {self.config_path}: {e}")

                with open(self.config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)

                # Validate loaded fields
                validated = {}
                for key, default_val in self.DEFAULT_CONFIG.items():
                    val = data.get(key, default_val)
                    if self._validate_field(key, val):
                        validated[key] = val
                    else:
                        logger.warning(f"Invalid config value detected for key '{key}'. Reverting to default.")
                        validated[key] = default_val

                self.config = validated
            except Exception as e:
                logger.error(f"Error reading config file: {e}. Resetting to secure defaults.")
                self.config = copy.deepcopy(self.DEFAULT_CONFIG)
                self._save_under_lock()

    def _save_under_lock(self):
        """Saves configuration securely using owner-only permissions (0600)."""
        try:
            # Write configuration securely to temporary file first, then replace atomatically if possible
            # or directly write and apply chmod immediately.
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=4)
            
            # Enforce 0600 permissions (read/write only by owner) to prevent unauthorized read/tamper
            os.chmod(self.config_path, 0o600)
        except Exception as e:
            logger.error(f"Failed to save configuration securely: {e}")

    def save(self):
        """Thread-safe public save method."""
        with self.lock:
            self._save_under_lock()

    def _validate_field(self, key, value):
        """
        Validates configuration fields.
        Specifically restricts IP addresses to private networks to prevent unauthorized data routing.
        """
        if key == "selected_edge":
            return value in ("LEFT", "RIGHT", "TOP", "BOTTOM")
        
        elif key == "edge_friction_ms":
            return isinstance(value, int) and 0 <= value <= 10000
        
        elif key == "connection_mode":
            return value in ("WIFI", "USB")
        
        elif key == "saved_devices":
            if not isinstance(value, list):
                return False
            for dev in value:
                if not isinstance(dev, dict):
                    return False
                if not all(k in dev for k in ("device_name", "last_known_ip", "preferred_port")):
                    return False
                
                # Enforce private IP validation to prevent DNS-rebinding or scraping setups
                ip = dev["last_known_ip"]
                try:
                    ip_obj = ipaddress.ip_address(ip)
                    if not (ip_obj.is_private or ip_obj.is_loopback):
                        logger.warning(f"Security Warning: IP address {ip} is not private/local. Rejecting.")
                        return False
                except ValueError:
                    logger.warning(f"Invalid IP format: {ip}")
                    return False

                # Validate port bounds
                port = dev["preferred_port"]
                if not (isinstance(port, int) and 1 <= port <= 65535):
                    return False
            return True
        return False

    # Getters
    def get_selected_edge(self):
        with self.lock:
            return self.config.get("selected_edge", "RIGHT")

    def get_edge_friction_ms(self):
        with self.lock:
            return self.config.get("edge_friction_ms", 150)

    def get_connection_mode(self):
        with self.lock:
            return self.config.get("connection_mode", "WIFI")

    def get_saved_devices(self):
        with self.lock:
            return copy.deepcopy(self.config.get("saved_devices", []))

    # Setters
    def set_selected_edge(self, edge):
        if not self._validate_field("selected_edge", edge):
            raise ValueError(f"Invalid edge: {edge}. Must be one of LEFT, RIGHT, TOP, BOTTOM.")
        with self.lock:
            self.config["selected_edge"] = edge
            self._save_under_lock()

    def set_edge_friction_ms(self, ms):
        if not self._validate_field("edge_friction_ms", ms):
            raise ValueError("Invalid friction duration. Must be an int between 0 and 10000.")
        with self.lock:
            self.config["edge_friction_ms"] = ms
            self._save_under_lock()

    def set_connection_mode(self, mode):
        if not self._validate_field("connection_mode", mode):
            raise ValueError("Invalid connection mode. Must be 'WIFI' or 'USB'.")
        with self.lock:
            self.config["connection_mode"] = mode
            self._save_under_lock()

    def set_saved_devices(self, devices):
        if not self._validate_field("saved_devices", devices):
            raise ValueError("Invalid devices list. Saved devices must contain valid keys and private IPs.")
        with self.lock:
            self.config["saved_devices"] = copy.deepcopy(devices)
            self._save_under_lock()
