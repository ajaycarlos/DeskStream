# UI/UX Specification

## Desktop UI Design (Linux Mint)
* **Form Factor:** Compact, distraction-free control window designed to blend naturally with the system theme.
* **System Tray Integration:** Minimize-to-tray capability showing real-time connection state icons (Connected, Searching, Disconnected).
* **Edge Configuration Interface:** A simple visual selector allowing users to click on the Left, Right, Top, or Bottom boundary of a simulated screen diagram to lock down the device orientation.

## Android Mobile UI Design (Jetpack Compose)
* **Modern Theme Structure:** Clean layout styled with a clean design system, fully supporting dark/light mode switching.
* **Connection Wizard:** A straightforward main dashboard displaying current connection status, active IP/Port readouts, and toggle buttons for USB vs. Wireless mode.
* **Setup Checklist UI:** Visual indicator badges alerting users if the mandatory system permissions (Accessibility Service and Input Method Service) are fully authorized or require action.

## Transition Ergonomics
* **Edge Resistance Friction:** A customizable boundary delay parameter (in milliseconds) requiring a deliberate push against the screen perimeter to eliminate accidental workspace transitions.
* **Visual Return Clues:** Subtle screen perimeter flashes or vibrations when crossing boundaries to provide immediate tactile or visual confirmation to the user.
