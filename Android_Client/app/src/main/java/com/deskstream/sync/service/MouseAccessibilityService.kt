package com.deskstream.sync.service

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.graphics.Path
import android.graphics.PixelFormat
import android.util.Log
import android.view.Gravity
import android.view.View
import android.view.WindowManager
import android.widget.ImageView
import com.deskstream.sync.R
import com.deskstream.sync.event.InputEvent
import com.deskstream.sync.event.InputEventBus
import kotlinx.coroutines.*

/**
 * MouseAccessibilityService leverages Android's Accessibility overlay features to render a
 * virtual system cursor on top of all applications, translate incoming coordinate stream updates
 * into screen movements, and inject gestures (such as taps) programmatically.
 */
class MouseAccessibilityService : AccessibilityService() {

    private lateinit var windowManager: WindowManager
    private lateinit var cursorView: ImageView
    private lateinit var layoutParams: WindowManager.LayoutParams

    private val serviceScope = CoroutineScope(Dispatchers.Main + SupervisorJob())

    // Screen dimension limits
    private var screenWidth = 0
    private var screenHeight = 0

    // Local coordinates cache
    private var currentX = 0
    private var currentY = 0
    private var isCursorActive = false

    // Track state of drag/stroke continuation
    private var activeStroke: GestureDescription.StrokeDescription? = null
    private var dragPath: Path? = null
    private var lastDragTime = 0L

    override fun onServiceConnected() {
        super.onServiceConnected()
        Log.i(TAG, "MouseAccessibilityService connected.")

        windowManager = getSystemService(WINDOW_SERVICE) as WindowManager

        // Read active display metrics for boundary clipping
        val metrics = resources.displayMetrics
        screenWidth = metrics.widthPixels
        screenHeight = metrics.heightPixels

        // Center coordinates by default
        currentX = screenWidth / 2
        currentY = screenHeight / 2

        initializeCursorOverlay()
        startInputEventCollection()
    }

    override fun onAccessibilityEvent(event: android.view.accessibility.AccessibilityEvent?) {
        // No-op: We only use this service for gesture injection and overlays.
    }

    override fun onInterrupt() {
        Log.w(TAG, "Accessibility Service Interrupted.")
    }

    /**
     * Set up the floating mouse cursor overlay using WindowManager.
     */
    private fun initializeCursorOverlay() {
        cursorView = ImageView(this).apply {
            setImageResource(R.drawable.ic_mouse_cursor)
            visibility = View.GONE // Start hidden until client connects and moves mouse
        }

        val cursorSizePx = (24 * resources.displayMetrics.density).toInt()

        layoutParams = WindowManager.LayoutParams().apply {
            width = cursorSizePx
            height = cursorSizePx
            type = WindowManager.LayoutParams.TYPE_ACCESSIBILITY_OVERLAY
            flags = WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or
                    WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE or
                    WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS
            format = PixelFormat.TRANSLUCENT
            gravity = Gravity.TOP or Gravity.START
            x = currentX
            y = currentY
        }

        try {
            windowManager.addView(cursorView, layoutParams)
            Log.d(TAG, "Cursor overlay added to WindowManager.")
        } catch (e: Exception) {
            Log.e(TAG, "Failed to add cursor view to window: ${e.message}", e)
        }
    }

    /**
     * Collects and processes KVM input events from the centralized InputEventBus.
     */
    private fun startInputEventCollection() {
        serviceScope.launch {
            InputEventBus.events.collect { event ->
                when (event) {
                    is InputEvent.MouseMove -> handleMouseMove(event.dx, event.dy)
                    is InputEvent.MouseClick -> handleMouseClick(event.button, event.state)
                    else -> {} // Keystrokes are handled by the custom InputMethodService (IME)
                }
            }
        }
    }

    /**
     * Moves the overlay coordinates and monitors edge-unlock threshold breaches.
     */
    private fun handleMouseMove(dx: Int, dy: Int) {
        // Activate cursor overlay if it was hidden
        if (!isCursorActive) {
            isCursorActive = true
            cursorView.visibility = View.VISIBLE
            // Position the cursor slightly offset from the left edge when entering from the PC monitor
            currentX = 15
            currentY = screenHeight / 2
            Log.d(TAG, "Cursor activated. Entry coordinates: ($currentX, $currentY)")
        }

        // Apply relative movements, clamping to physical screen bounds
        currentX = (currentX + dx).coerceIn(0, screenWidth)
        currentY = (currentY + dy).coerceIn(0, screenHeight)

        // EDGE BOUNDARY TRAP: If pushed past the left edge, trigger PC mouse release
        if (currentX <= 0) {
            triggerEdgeUnlock()
            return
        }

        // Update layout coordinates on the UI main thread
        layoutParams.x = currentX
        layoutParams.y = currentY
        
        try {
            if (cursorView.parent != null) {
                windowManager.updateViewLayout(cursorView, layoutParams)
            }
        } catch (e: Exception) {
            Log.e(TAG, "Error updating cursor overlay layout: ${e.message}")
        }
    }

    /**
     * Dispatches click and swipe gestures based on the physical mouse button states.
     */
    private fun handleMouseClick(button: String, state: Int) {
        if (!isCursorActive) return

        if (button == "LEFT") {
            // STATE: 1 = Down (Press), 0 = Up (Release)
            if (state == 1) {
                // Instantly inject a tap at current cursor coordinate on press down for zero latency
                injectTap(currentX.toFloat(), currentY.toFloat())
            }
        }
    }

    /**
     * Triggers the release of the PC mouse trap. Hides the Android cursor.
     */
    private fun triggerEdgeUnlock() {
        isCursorActive = false
        cursorView.visibility = View.GONE
        Log.i(TAG, "Cursor exited Android screen edge. Requesting PC unlock.")
        
        serviceScope.launch {
            InputEventBus.requestUnlock()
        }
    }

    /**
     * Programmatically simulates a physical tap gesture using Android's Accessibility framework.
     */
    private fun injectTap(x: Float, y: Float) {
        val path = Path().apply {
            moveTo(x, y)
        }
        
        // Construct and dispatch a short 60ms click stroke
        val stroke = GestureDescription.StrokeDescription(path, 0, 60)
        val gesture = GestureDescription.Builder()
            .addStroke(stroke)
            .build()

        try {
            dispatchGesture(gesture, object : GestureResultCallback() {
                override fun onCompleted(gestureDescription: GestureDescription?) {
                    super.onCompleted(gestureDescription)
                    Log.d(TAG, "Tap injected successfully at ($x, $y).")
                }

                override fun onCancelled(gestureDescription: GestureDescription?) {
                    super.onCancelled(gestureDescription)
                    Log.w(TAG, "Tap injection cancelled at ($x, $y).")
                }
            }, null)
        } catch (e: Exception) {
            Log.e(TAG, "Failed to dispatch tap gesture: ${e.message}")
        }
    }

    override fun onDestroy() {
        Log.i(TAG, "MouseAccessibilityService destroyed.")
        try {
            if (::cursorView.isInitialized && cursorView.parent != null) {
                windowManager.removeView(cursorView)
            }
        } catch (e: Exception) {
            Log.e(TAG, "Error removing cursor view on destroy: ${e.message}")
        }
        serviceScope.cancel()
        super.onDestroy()
    }

    companion object {
        private const val TAG = "MouseAccessibilityService"
    }
}
