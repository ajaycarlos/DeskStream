# System Architecture

The application relies on a local Client-Server model. The PC Host captures hardware device inputs at the system level and routes those instructions straight to the listening Android handset.

+------------------------------------------------------------+
|                   DESKTOP HOST (PC)                        |
|                                                            |
|  +------------------+     +-----------------------------+  |
|  |  pynput Hooks    | --> | Edge Checker Logic          |  |
|  |  (Mouse/Keyboard)|     | (Is past threshold screen?) |  |
|  +------------------+     +-----------------------------+  |
|                                         |                  |
|                                         v (Yes)            |
|                           +-----------------------------+  |
|                           | Network/USB Streaming Engine|  |
|                           +-----------------------------+  |
+-----------------------------------------|------------------+
|
[ Local Connection Layer ]
(ADB Port Tunnel OR Wi-Fi)
|
+-----------------------------------------v------------------+
|                   MOBILE CLIENT (ANDROID)                  |
|                                                            |
|                           +-----------------------------+  |
|                           | Background TCP/UDP Listener |  |
|                           +-----------------------------+  |
|                                         |                  |
|                    +--------------------+----------------+ |
|                    | (Route Mouse Data) | (Route Keys)   | |
|                    v                    v                |
|  +--------------------+     +----------------------------+ |
|  | Accessibility      |     | InputMethodService         | |
|  | Service (Cursor    |     | (Invisible Keyboard        | |
|  | Overlay / Taps)    |     | System Text Injection)     | |
|  +--------------------+     +----------------------------+ |
+------------------------------------------------------------+


## System Workflow Breakdown
1.  **Input Capturing:** The Python host interceptor monitors hardware events. If mouse tracking reveals coordinates within normal bounds, it skips overriding.
2.  **Boundary Interception:** The moment coordinates breach the designated exit edge, the cursor hooks intercept system execution, parking the visible desktop cursor at the screen perimeter.
3.  **Stream Dispatch:** Input deltas map into the local socket interface, immediate and raw.
4.  **Target Consumer Processing:** The Android service continuously processes incoming data packets. Mouse structures go to the `AccessibilityService` context to adjust the floating UI cursor overlay, while keystrokes land in the custom input system window.
