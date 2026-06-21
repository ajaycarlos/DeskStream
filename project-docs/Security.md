# Security & Privacy Protocol

Because this tool bypasses native system sandboxes to simulate inputs, enforcing rigid security profiles locally is paramount.

## Local Security Controls
* **Explicit IP Device Pinning:** The Android application rejects any incoming wireless connections whose source IP addresses do not precisely match the user's white-listed PC address configured during device pairing.
* **Single-Active Connection Enforcement:** Socket bindings are limited to a single concurrent listener. New handshake requests are automatically ignored while an active mouse session is engaged.
* **Local Address Isolation:** Socket binding explicitly avoids open public routing boards. Communication targets are locked to local private networks (`192.168.x.x`, `10.x.x.x`) or direct interface channels (`127.0.0.1` for ADB USB connections).
* **No Persistent Data Aggregation:** Input stream instructions are parsed strictly on the fly inside volatile memory buffers. Keystrokes, text inputs, coordinates, and macro events are discarded immediately after execution to prevent local keystroke harvesting.
