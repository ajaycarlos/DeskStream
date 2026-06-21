# Product Requirement Document (PRD) - DeskStream Sync

## Project Name
DeskStream Sync (Seamless PC-to-Android Input Bridge)

## Problem Statement
Users frequently work with both a PC and an Android mobile device side-by-side on their desks. Switching back and forth between a physical PC keyboard/mouse and a touchscreen breaks focus, causes physical fatigue, and slows down productivity. Existing solutions are either locked behind paid upgrades, require device rooting, or lack modern, high-performance UI components.

## Target Users
* Software developers and multi-device power users.
* Professionals who use their Android phones or tablets as secondary monitoring or messaging screens alongside a Linux desktop.

## Core Value Proposition
A 100% free, private, low-latency, and open-source software KVM that turns an Android device into a seamless physical extension of the desktop workspace.

## Core Features
1.  **Seamless Boundary Handoff:** Moving the mouse cursor past a designated PC screen edge hides the PC cursor and activates a virtual cursor on Android.
2.  **Dual Connection Protocol:** High-stability USB link via ADB port forwarding or wireless convenience over local Wi-Fi sockets.
3.  **Virtual Mouse Injection:** Smooth cursor overlay drawing and tap/drag/scroll injection on Android without root.
4.  **Invisible Keyboard Forwarding:** Native text injection into Android fields using the PC keyboard when the phone is focused.
5.  **Local-Only Operation:** Complete data privacy and near-zero latency by executing all transfers strictly peer-to-peer.

## User Flow
1.  **Setup:** User opens the Linux Mint Python desktop application and the Jetpack Compose Android client application.
2.  **Pairing:** User selects either USB or Wi-Fi connection mode and initializes the link.
3.  **Alignment:** User configures which physical screen edge (e.g., Right) matches the Android device's location.
4.  **Execution:** User pushes the PC mouse to the right edge. The cursor seamlessly transitions to the Android display. Typing on the PC keyboard sends text directly to the active Android text field.
5.  **Return:** User moves the Android cursor back past the left edge, restoring desktop input control immediately.
