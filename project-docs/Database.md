# Storage & Local Configuration Schema

*Note: This architecture operates completely serverless and cloudless. Persistence is restricted to local configuration preferences stored directly on each device.*

## Desktop Preferences Store (`config.json`)
```json
{
  "selected_edge": "RIGHT",
  "edge_friction_ms": 150,
  "connection_mode": "WIFI",
  "saved_devices": [
    {
      "device_name": "Android Mobile Device",
      "last_known_ip": "192.168.1.105",
      "preferred_port": 8080
    }
  ]
}

Android Preferences Store (SharedPreferences / Datastore)
pref_connection_mode: [String] "USB" or "WIFI".

pref_allowed_host_ip: [String] Stored IP address of the authorized paired computer.

pref_ime_enabled: [Boolean] System status cache tracking if the custom keyboard input method is active.
