package com.deskstream.sync.service

import android.inputmethodservice.InputMethodService
import android.util.Log
import android.view.KeyEvent
import android.view.View
import android.view.ViewGroup
import android.widget.FrameLayout
import com.deskstream.sync.event.InputEvent
import com.deskstream.sync.event.InputEventBus
import kotlinx.coroutines.*

/**
 * KeyboardInjectionService is a zero-UI InputMethodService (IME) that listens for keyboard
 * events from the PC Host and commits them directly into the current active Android text container.
 */
class KeyboardInjectionService : InputMethodService() {

    private val serviceScope = CoroutineScope(Dispatchers.Main + SupervisorJob())

    override fun onCreate() {
        super.onCreate()
        Log.i(TAG, "KeyboardInjectionService created.")
        startInputEventCollection()
    }

    /**
     * Creates an empty, transparent view of size 0 to prevent the system soft keyboard
     * from taking up physical screen space.
     */
    override fun onCreateInputView(): View {
        val root = FrameLayout(this)
        root.layoutParams = ViewGroup.LayoutParams(0, 0)
        
        // Disable soft input layout display entirely
        return root
    }

    override fun onStartInputView(info: android.view.inputmethod.EditorInfo?, restarting: Boolean) {
        super.onStartInputView(info, restarting)
        // Explicitly hide the soft keyboard window frame
    }

    /**
     * Listens for incoming keyboard stream messages via the central EventBus.
     */
    private fun startInputEventCollection() {
        serviceScope.launch {
            InputEventBus.events.collect { event ->
                when (event) {
                    is InputEvent.KeyText -> injectText(event.text)
                    is InputEvent.KeyAction -> injectKeyAction(event.action)
                    else -> {} // Mouse movements/clicks are routed to MouseAccessibilityService
                }
            }
        }
    }

    /**
     * Commits normal alphanumeric text streams into the focused edit container.
     */
    private fun injectText(text: String) {
        val conn = currentInputConnection
        if (conn != null) {
            conn.commitText(text, 1)
            Log.d(TAG, "Successfully injected text: '$text'")
        } else {
            Log.w(TAG, "Text injection dropped: No active InputConnection established.")
        }
    }

    /**
     * Simulates physical hardware key taps (press & release) for navigation or special action keys.
     */
    private fun injectKeyAction(action: String) {
        val conn = currentInputConnection
        if (conn == null) {
            Log.w(TAG, "Key action '$action' dropped: No active InputConnection established.")
            return
        }

        val keyCode = mapActionToKeyCode(action)
        if (keyCode == KeyEvent.KEYCODE_UNKNOWN) {
            Log.w(TAG, "Unrecognized key action code: '$action'")
            return
        }

        // Send Key Down event followed immediately by Key Up event to emulate a complete physical press
        conn.sendKeyEvent(KeyEvent(KeyEvent.ACTION_DOWN, keyCode))
        conn.sendKeyEvent(KeyEvent(KeyEvent.ACTION_UP, keyCode))
        Log.d(TAG, "Successfully injected keycode: $keyCode (Action: '$action')")
    }

    /**
     * Map physical PC desktop keys to standard Android KeyEvents.
     */
    private fun mapActionToKeyCode(action: String): Int {
        return when (action.uppercase().trim()) {
            "BACKSPACE", "DELETE" -> KeyEvent.KEYCODE_DEL
            "ENTER", "RETURN" -> KeyEvent.KEYCODE_ENTER
            "TAB" -> KeyEvent.KEYCODE_TAB
            "SPACE" -> KeyEvent.KEYCODE_SPACE
            "ESCAPE", "ESC" -> KeyEvent.KEYCODE_ESCAPE
            "LEFT", "ARROW_LEFT", "DPAD_LEFT" -> KeyEvent.KEYCODE_DPAD_LEFT
            "RIGHT", "ARROW_RIGHT", "DPAD_RIGHT" -> KeyEvent.KEYCODE_DPAD_RIGHT
            "UP", "ARROW_UP", "DPAD_UP" -> KeyEvent.KEYCODE_DPAD_UP
            "DOWN", "ARROW_DOWN", "DPAD_DOWN" -> KeyEvent.KEYCODE_DPAD_DOWN
            "HOME" -> KeyEvent.KEYCODE_MOVE_HOME
            "END" -> KeyEvent.KEYCODE_MOVE_END
            "PAGE_UP" -> KeyEvent.KEYCODE_PAGE_UP
            "PAGE_DOWN" -> KeyEvent.KEYCODE_PAGE_DOWN
            else -> KeyEvent.KEYCODE_UNKNOWN
        }
    }

    override fun onDestroy() {
        Log.i(TAG, "KeyboardInjectionService destroyed.")
        serviceScope.cancel()
        super.onDestroy()
    }

    companion object {
        private const val TAG = "KeyboardInjectionService"
    }
}
