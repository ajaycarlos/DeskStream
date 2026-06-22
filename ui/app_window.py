import tkinter as tk
from tkinter import ttk, messagebox
import logging

logger = logging.getLogger("deskstream.ui.window")

class ConfigWindow:
    """
    Tkinter-based configuration window for DeskStream Sync.
    Allows editing screen edge alignment, friction timing, connection protocol modes,
    and the live mouse sensitivity multiplier.
    """
    def __init__(self, settings_manager, on_save_callback=None, mouse_hook_manager=None):
        self.settings = settings_manager
        self.on_save_callback = on_save_callback
        # Optional live reference to the running MouseHookManager for real-time updates.
        self.mouse_hook_manager = mouse_hook_manager
        self.is_closed = False

        self.root = tk.Tk()
        self.root.title("DeskStream Sync - Settings")
        self.root.geometry("420x420")
        self.root.resizable(False, False)

        # Set window close intercept
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        # Main container with margins
        main_frame = ttk.Frame(self.root, padding="20 20 20 20")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # Title Header
        title_label = ttk.Label(
            main_frame,
            text="DeskStream Host Configuration",
            font=("Helvetica", 14, "bold")
        )
        title_label.grid(row=0, column=0, columnspan=2, pady=(0, 20), sticky=tk.W)

        # 1. Edge Selection Dropdown
        ttk.Label(main_frame, text="Active PC Screen Edge:").grid(row=1, column=0, pady=10, sticky=tk.W)
        self.edge_var = tk.StringVar(value=self.settings.get_selected_edge())
        self.edge_dropdown = ttk.Combobox(
            main_frame,
            textvariable=self.edge_var,
            values=["LEFT", "RIGHT", "TOP", "BOTTOM"],
            state="readonly",
            width=15
        )
        self.edge_dropdown.grid(row=1, column=1, pady=10, sticky=tk.E)

        # 2. Edge Friction Slider
        ttk.Label(main_frame, text="Edge Friction (ms):").grid(row=2, column=0, pady=10, sticky=tk.W)
        self.friction_val_label = ttk.Label(main_frame, text=f"{self.settings.get_edge_friction_ms()} ms")
        self.friction_val_label.grid(row=2, column=1, sticky=tk.E)
        
        self.friction_var = tk.IntVar(value=self.settings.get_edge_friction_ms())
        self.friction_slider = ttk.Scale(
            main_frame,
            from_=0,
            to=2000,
            orient=tk.HORIZONTAL,
            variable=self.friction_var,
            command=self._on_slider_move
        )
        self.friction_slider.grid(row=3, column=0, columnspan=2, pady=(0, 15), sticky="ew")

        # 3. Mouse Sensitivity Slider
        ttk.Label(main_frame, text="Mouse Sensitivity:").grid(row=4, column=0, pady=10, sticky=tk.W)
        self.sensitivity_val_label = ttk.Label(
            main_frame,
            text=f"{self.settings.get_mouse_sensitivity():.2f}×"
        )
        self.sensitivity_val_label.grid(row=4, column=1, sticky=tk.E)

        self.sensitivity_var = tk.DoubleVar(value=self.settings.get_mouse_sensitivity())
        self.sensitivity_slider = ttk.Scale(
            main_frame,
            from_=0.1,
            to=3.0,
            orient=tk.HORIZONTAL,
            variable=self.sensitivity_var,
            command=self._on_sensitivity_slider_move
        )
        self.sensitivity_slider.grid(row=5, column=0, columnspan=2, pady=(0, 15), sticky="ew")

        # 4. Connection Mode Radios
        ttk.Label(main_frame, text="Connection Transport:").grid(row=6, column=0, pady=10, sticky=tk.W)
        self.conn_var = tk.StringVar(value=self.settings.get_connection_mode())
        
        radio_frame = ttk.Frame(main_frame)
        radio_frame.grid(row=6, column=1, pady=10, sticky=tk.E)
        
        ttk.Radiobutton(
            radio_frame,
            text="Wi-Fi (Wireless)",
            value="WIFI",
            variable=self.conn_var
        ).pack(side=tk.LEFT, padx=5)
        
        ttk.Radiobutton(
            radio_frame,
            text="USB (ADB Link)",
            value="USB",
            variable=self.conn_var
        ).pack(side=tk.LEFT, padx=5)

        # Spacer row
        main_frame.grid_rowconfigure(7, minsize=20)

        # 5. Save & Close Button
        self.save_btn = ttk.Button(
            main_frame,
            text="Save & Apply Settings",
            command=self._on_save
        )
        self.save_btn.grid(row=8, column=0, columnspan=2, pady=10, sticky="ew")

        # Keep window on top for accessibility focus
        self.root.attributes("-topmost", True)

    def _on_slider_move(self, value):
        """Updates the friction slider text label on move."""
        ms = int(float(value))
        self.friction_val_label.config(text=f"{ms} ms")

    def _on_sensitivity_slider_move(self, value):
        """
        Fires on every slider tick.  Updates the live label AND immediately pushes
        the new multiplier into the running MouseHookManager (thread-safe via
        set_sensitivity()).  The engine responds to the very next mouse event —
        no reconnect or restart needed.
        """
        val = round(float(value), 2)
        self.sensitivity_val_label.config(text=f"{val:.2f}×")
        if self.mouse_hook_manager is not None:
            self.mouse_hook_manager.set_sensitivity(val)

    def _on_save(self):
        """Validates settings changes, writes back to config.json, and closes window."""
        selected_edge = self.edge_var.get()
        friction_ms = int(self.friction_var.get())
        conn_mode = self.conn_var.get()
        sensitivity = round(float(self.sensitivity_var.get()), 2)

        try:
            # Update settings via SettingsManager (enforces secure types/bounds validation)
            self.settings.set_selected_edge(selected_edge)
            self.settings.set_edge_friction_ms(friction_ms)
            self.settings.set_connection_mode(conn_mode)
            self.settings.set_mouse_sensitivity(sensitivity)
            
            logger.info("Settings saved successfully and written to config.json.")
            
            # Fire callback to alert main loop to restart services with new parameters
            if self.on_save_callback:
                self.on_save_callback()

            messagebox.showinfo("Success", "Settings applied successfully!")
            self.close()
        except Exception as e:
            logger.error(f"Error saving settings: {e}")
            messagebox.showerror("Error", f"Failed to save settings: {e}")

    def update(self):
        """Runs one tick of the Tkinter event loop. Called by the main loop."""
        if not self.is_closed:
            try:
                self.root.update()
            except tk.TclError:
                # Occurs if the window was closed externally
                self.is_closed = True

    def close(self):
        """Safely shuts down the Tkinter window instance."""
        if not self.is_closed:
            self.is_closed = True
            try:
                self.root.destroy()
            except Exception as e:
                logger.debug(f"Error destroying config window: {e}")

    def focus(self):
        """Brings settings window to foreground focus."""
        if not self.is_closed:
            self.root.deiconify()
            self.root.lift()
            self.root.attributes("-topmost", True)
