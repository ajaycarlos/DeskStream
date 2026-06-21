# Local Communication Protocol & Core Actions

Since this system uses peer-to-peer sockets instead of standard HTTP web endpoints, communication is defined by structured data packets sent over the local network connection.

## Data Packet Definitions

### 1. Pointer Coordinates Move
* **Identifier:** `MOUSE_MOVE`
* **Payload Layout:** `M:[dX]:[dY]`
* **Example Payload:** `M:12:-4` (Tells the Android app to advance the cursor position 12 units on the X-axis and -4 units on the Y-axis).

### 2. Physical Pointer Click
* **Identifier:** `MOUSE_CLICK`
* **Payload Layout:** `C:[BUTTON]:[STATE]` (STATE: 1 = Press down, 0 = Release up)
* **Example Payload:** `C:LEFT:1` (Executes a left-click action on the current coordinates).

### 3. Keyboard Text Payload
* **Identifier:** `KEY_TEXT`
* **Payload Layout:** `K:TEXT:[STRING]`
* **Example Payload:** `K:TEXT:Hello World` (Instructs the custom IME keyboard service to instantly type out the string).

### 4. Special System Key State
* **Identifier:** `KEY_ACTION`
* **Payload Layout:** `K:ACT:[KEY_CODE]`
* **Example Payload:** `K:ACT:BACKSPACE` (Triggers a backspace delete action inside the current active text container).
