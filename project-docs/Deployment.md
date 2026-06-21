# Environment Setup & Local Installation

## Host Machine Build Steps (Linux Mint)
1.  **Install Python Environments:** Ensure Python 3.10+ and standard pip tools are installed on the workstation.
2.  **Verify X11 System Tools:** Verify accessibility permissions for system event reading by installing the development dependencies:
    ```bash
    sudo apt-get install python3-dev libx11-dev
    ```
3.  **Dependency Setup:** Install project library dependencies:
    ```bash
    pip install pynput
    ```
4.  **Launch Scripts:** Run the server application background loop:
    ```bash
    python main.py
    ```

## Target Device Build Steps (Android Mobile)
1.  **Environment Configuration:** Open the project repository inside Android Studio or your preferred terminal pipeline.
2.  **Compile & Run:** Build and deploy the unsigned debug APK artifact onto the physical device.
3.  **Grant System Permissions:**
    * Navigate to *Settings -> Accessibility* and activate the application's Custom Cursor Service toggle.
    * Navigate to *Settings -> Language & Input -> On-Screen Keyboard* and enable the application's Invisible Input Bridge Keyboard choice.
