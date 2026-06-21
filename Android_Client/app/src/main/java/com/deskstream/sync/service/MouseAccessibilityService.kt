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
 * MouseAccessibilityService leverages Android's Accessibility overlay to render a virtual
 * cursor on top of all applications, translate incoming delta streams into screen movements,
 * and inject tap/scroll gestures programmatically.
 *
 * FIX NOTES (invisible cursor + accidental scroll):
 *
 * 1. CURSOR NEVER APPEARED – root cause was a combination of three issues:
 *    a) layoutParams.x / .y with Gravity.TOP|START means the WindowManager
 *       interprets x/y as pixel offsets from the TOP-LEFT corner of the screen.
 *       The original code assigned currentX (an absolute pixel) directly, which
 *       is correct, but the initial values (screenWidth/2, screenHeight/2) put
 *       the cursor dead-centre on first show.  The real problem was that
 *       cursorView.visibility was set to View.GONE at creation and was ONLY made
 *       visible inside handleMouseMove().  On the Redmi 9i, the first few delta
 *       packets arrived before isCursorActive was set, so handleMouseMove() was
 *       racing against itself and the visibility set was missed.  Fixed by
 *       exposing a separate showCursor() method that is called once, reliably.
 *    b) displayMetrics.widthPixels / heightPixels returns the FULL screen size
 *       including system bars on some OEM ROMs.  We now use
 *       WindowManager.currentWindowMetrics() on API 30+ so the drawable area
 *       matches what the overlay sees.
 *    c) TYPE_ACCESSIBILITY_OVERLAY does NOT need SYSTEM_ALERT_WINDOW permission,
 *       but it DOES require that the accessibility service is granted the overlay
 *       capability in its config XML (canRetrieveWindowContent is not needed;
 *       canPerformGestures is needed for gesture injection).  Both were present,
 *       so this was not the issue – but we add an explicit log on addView success
 *       to make debugging easier in future.
 *
 * 2. ACCIDENTAL SCROLLING – scroll events on Android were being driven by the
 *    "treadmill" deltas from _on_scroll() in the Python host.  The original host
 *    code had no scroll forwarding, but the test run apparently triggered it via
 *    an unintended code path.  The Android side now handles a dedicated
 *    InputEvent.Scroll event type.  Meanwhile, the host's _on_scroll() sends a
 *    structured scroll delta (not reusing the move callback) so the two code
 *    paths can never be confused.  On the Android side, scroll is injected as a
 *    GestureDescription swipe (not a treadmill position update) so it does not
 *    move the cursor.
 *
 * 3. GESTURE INJECTION – a zero-length Path (moveTo only, no lineTo) is
 *    technically valid for a tap but some vendor gesture recognisers reject it.
 *    We now add a 1-pixel lineTo to make it an unambiguous single-point stroke.
 */
class MouseAccessibilityService : AccessibilityService() {

    private lateinit var windowManager: WindowManager
    private var cursorView: ImageView? = null
    private lateinit var layoutParams: WindowManager.LayoutParams

    private val serviceScope = CoroutineScope(Dispatchers.Main + SupervisorJob())

    // Screen dimension limits (drawable area, not including system bars)
    private var screenWidth = 0
    private var screenHeight = 0

    // Current virtual cursor position in physical screen pixels
    private var currentX = 0
    private var currentY = 0
    private var isCursorActive = false

    override fun onServiceConnected() {
        super.onServiceConnected()
        Log.i(TAG, "MouseAccessibilityService connected.")

        windowManager = getSystemService(WINDOW_SERVICE) as WindowManager

        // ── FIX: use current window metrics for correct drawable area ─────────
        if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.R) {
            val metrics = windowManager.currentWindowMetrics
            screenWidth = metrics.bounds.width()
            screenHeight = metrics.bounds.height()
        } else {
            @Suppress("DEPRECATION")
            val dm = resources.displayMetrics
            screenWidth = dm.widthPixels
            screenHeight = dm.heightPixels
        }

        Log.i(TAG, "Screen dimensions: ${screenWidth}x${screenHeight}")

        // Start cursor at a sensible entry position (slightly inside left edge)
        currentX = 15
        currentY = screenHeight / 2

        initializeCursorOverlay()
        startInputEventCollection()
    }

    override fun onAccessibilityEvent(event: android.view.accessibility.AccessibilityEvent?) {
        // No-op: we only use this service for gesture injection and overlays.
    }

    override fun onInterrupt() {
        Log.w(TAG, "Accessibility Service interrupted.")
    }

    // ── Cursor overlay ───────────────────────────────────────────────────────

    /**
     * Set up the floating cursor overlay using WindowManager.
     * The view starts hidden (View.GONE) and is shown only when the first
     * mouse-move delta arrives via showCursor().
     */
    private fun initializeCursorOverlay() {
        val view = ImageView(this).apply {
            setImageResource(R.drawable.ic_mouse_cursor)
            visibility = View.GONE
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
            // Gravity.TOP|START: x/y are pixel offsets from the top-left corner
            gravity = Gravity.TOP or Gravity.START
            x = currentX
            y = currentY
        }

        try {
            windowManager.addView(view, layoutParams)
            cursorView = view
            Log.i(TAG, "Cursor overlay added to WindowManager at ($currentX, $currentY).")
        } catch (e: Exception) {
            Log.e(TAG, "Failed to add cursor view to WindowManager: ${e.message}", e)
        }
    }

    /**
     * Makes the cursor visible for the first time and updates its position.
     * Safe to call from the Main thread only (coroutines are dispatched on Main).
     */
    private fun showCursor() {
        val view = cursorView ?: return
        isCursorActive = true
        view.visibility = View.VISIBLE
        layoutParams.x = currentX
        layoutParams.y = currentY
        try {
            windowManager.updateViewLayout(view, layoutParams)
            Log.i(TAG, "Cursor shown at entry position ($currentX, $currentY).")
        } catch (e: Exception) {
            Log.e(TAG, "Error showing cursor: ${e.message}")
        }
    }

    // ── Event collection ─────────────────────────────────────────────────────

    private fun startInputEventCollection() {
        serviceScope.launch {
            InputEventBus.events.collect { event ->
                when (event) {
                    is InputEvent.MouseMove  -> handleMouseMove(event.dx, event.dy)
                    is InputEvent.MouseClick -> handleMouseClick(event.button, event.state)
                    is InputEvent.MouseScroll -> handleMouseScroll(event.dy)
                    else -> { /* Keystrokes handled by KeyboardInjectionService (IME) */ }
                }
            }
        }
    }

    // ── Input handlers ───────────────────────────────────────────────────────

    /**
     * Moves the overlay cursor by (dx, dy) pixels and checks the unlock boundary.
     */
    private fun handleMouseMove(dx: Int, dy: Int) {
        // ── FIX: show cursor on the very first move event, then update position
        if (!isCursorActive) {
            showCursor()
        }

        // Apply delta, clamp to screen bounds
        currentX = (currentX + dx).coerceIn(0, screenWidth - 1)
        currentY = (currentY + dy).coerceIn(0, screenHeight - 1)

        // Left-edge unlock: push cursor past left boundary to return control to PC
        if (currentX <= 0) {
            triggerEdgeUnlock()
            return
        }

        // Reposition overlay
        layoutParams.x = currentX
        layoutParams.y = currentY
        try {
            val view = cursorView
            if (view != null && view.parent != null) {
                windowManager.updateViewLayout(view, layoutParams)
            }
        } catch (e: Exception) {
            Log.e(TAG, "Error updating cursor overlay layout: ${e.message}")
        }
    }

    /**
     * Dispatches a tap gesture at the current cursor location.
     */
    private fun handleMouseClick(button: String, state: Int) {
        if (!isCursorActive) return

        if (button == "LEFT" && state == 1) {
            injectTap(currentX.toFloat(), currentY.toFloat())
        }
    }

    /**
     * Dispatches a vertical swipe gesture to scroll the content under the cursor.
     * dy > 0 = scroll down (content moves up); dy < 0 = scroll up (content moves down).
     */
    private fun handleMouseScroll(dy: Int) {
        if (!isCursorActive) return

        val x = currentX.toFloat()
        val y = currentY.toFloat()
        // Translate dy into a swipe distance: positive dy = fling upward on screen
        val swipeDistance = dy.toFloat().coerceIn(-400f, 400f)
        injectSwipe(x, y, x, y - swipeDistance, durationMs = 150)
    }

    // ── Unlock ───────────────────────────────────────────────────────────────

    private fun triggerEdgeUnlock() {
        isCursorActive = false
        cursorView?.visibility = View.GONE
        // Reset position for the next session
        currentX = 15
        currentY = screenHeight / 2
        Log.i(TAG, "Cursor exited Android screen edge. Requesting PC unlock.")
        serviceScope.launch {
            InputEventBus.requestUnlock()
        }
    }

    // ── Gesture helpers ──────────────────────────────────────────────────────

    /**
     * Injects a single-point tap gesture.
     * A 1-pixel lineTo is added to satisfy vendor gesture recognisers that reject
     * zero-length paths.
     */
    private fun injectTap(x: Float, y: Float) {
        val path = Path().apply {
            moveTo(x, y)
            lineTo(x + 1f, y + 1f)  // FIX: non-zero length path for vendor compat
        }
        val stroke = GestureDescription.StrokeDescription(path, 0L, 60L)
        val gesture = GestureDescription.Builder().addStroke(stroke).build()

        dispatchGesture(gesture, object : GestureResultCallback() {
            override fun onCompleted(gestureDescription: GestureDescription?) {
                Log.d(TAG, "Tap injected at ($x, $y).")
            }
            override fun onCancelled(gestureDescription: GestureDescription?) {
                Log.w(TAG, "Tap cancelled at ($x, $y).")
            }
        }, null)
    }

    /**
     * Injects a swipe gesture between two points for scroll simulation.
     */
    private fun injectSwipe(
        startX: Float, startY: Float,
        endX: Float, endY: Float,
        durationMs: Long = 150L
    ) {
        val path = Path().apply {
            moveTo(startX, startY)
            lineTo(endX, endY)
        }
        val stroke = GestureDescription.StrokeDescription(path, 0L, durationMs)
        val gesture = GestureDescription.Builder().addStroke(stroke).build()

        dispatchGesture(gesture, object : GestureResultCallback() {
            override fun onCompleted(gestureDescription: GestureDescription?) {
                Log.d(TAG, "Swipe injected ($startX,$startY)->($endX,$endY).")
            }
            override fun onCancelled(gestureDescription: GestureDescription?) {
                Log.w(TAG, "Swipe cancelled.")
            }
        }, null)
    }

    // ── Lifecycle ────────────────────────────────────────────────────────────

    override fun onDestroy() {
        Log.i(TAG, "MouseAccessibilityService destroyed.")
        val view = cursorView
        if (view != null) {
            try {
                if (view.parent != null) {
                    windowManager.removeView(view)
                }
            } catch (e: Exception) {
                Log.e(TAG, "Error removing cursor view on destroy: ${e.message}")
            }
            cursorView = null
        }
        serviceScope.cancel()
        super.onDestroy()
    }

    companion object {
        private const val TAG = "MouseAccessibilityService"
    }
}
