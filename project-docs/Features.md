# Features Specification

## 1. Host Application (PC Server)
* **Global Mouse/Keyboard Hooking:** Listens to global hardware input event streams without interfering with normal window activity.
* **Edge Capturing and Trapping:** Intercepts and stops native screen cursor rendering when standard boundaries are crossed.
* **Dynamic Resolution Scaling:** Translates absolute PC screen coordinates into relative resolution deltas tailored for the Android aspect ratio.

## 2. Client Application (Android Screen Overlay)
* **Custom Overlay Rendering:** Emulates a standard system mouse cursor using a persistent foreground visual layer.
* **Accessibility Action Processing:** Processes mouse click events and translates them programmatically into Android system gestures (taps, drags, long-presses).
* **Zero-UI Background Processing:** Hosts an Android foreground service to guarantee the connection stays active even when the screen dims or background execution limits apply.

## 3. Connection Profiles
* **ADB Tunneling Profile (USB):** Auto-configures an upstream socket forwarder using standard Android Debug Bridge protocol rules.
* **Local Socket Engine (Wi-Fi):** Performs local network device handshakes using low-latency UDP for coordinates and TCP for reliable keyboard state tracking.

## 4. Keyboard Forwarding Engine
* **Modifier Key Synchronization:** Monitors the active states of Shift, Ctrl, and Alt keys on the host machine to correctly process combinations on the destination device.
* **Background Input Context:** Injects text characters accurately using a custom system IME engine hook.
