# Technology Stack Selection

## Desktop Environment (Server)
* **Language:** Python 3.10+
* **Core Dependencies:**
    * `pynput` or `python-xlib` for global hardware hook capturing (optimized for X11/Wayland targets on Linux Mint).
    * `socket` (Built-in standard library for network socket management).
* **Packaging:** PyInstaller (for building a self-contained runtime executable).

## Mobile Environment (Client)
* **Language:** Kotlin
* **UI Framework:** Jetpack Compose (Modern declarative UI toolkit).
* **Target SDK:** Android SDK 31 (Android 12) through SDK 35.

## Data Transport & Communications
* **Wire Protocols:** Native TCP sockets for configuration handshakes and keyboard character feeds; UDP sockets for raw coordinate mouse updates.
* **Data Layout:** Light JSON text blocks or compact bit-packed byte arrays to minimize processing overhead and transmission latency.
