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
 * cursor on top of all applications and inject tap/drag/scroll gestures programmatically.
 *
 * ── Bug Fixes in this version ────────────────────────────────────────────────────────────
 *
 * BUG 2 – Resolution Mismatch / Premature Escape:
 *   After onServiceConnected() detects the real screen size, the service sends an
 *   "INIT:width:height\n" packet back to the Python host via InputEventBus so the
 *   host can dynamically clamp its virtual coordinate range to the real device bounds.
 *   Without this, the host used a hardcoded 1080×2400 clamp that may differ from the
 *   actual device, causing the virtual X counter to reach 0 (or overflow) sooner than
 *   expected, which triggered the premature "escape to PC" unlock.
 *
 * BUG 3 – Click Accuracy Offset (hot-spot misalignment):
 *   The ImageView overlay is sized at 24dp × 24dp and positioned with Gravity.TOP|START,
 *   so layoutParams.x/.y places its TOP-LEFT pixel on screen.  dispatchGesture() also
 *   receives absolute pixel coordinates.  For an arrow cursor whose tip is at the
 *   top-left of the image, both coordinates are consistent and taps should land exactly
 *   under the tip — BUT only if:
 *     a) The ImageView has NO internal padding (padding shifts the drawn image right/down
 *        without changing layoutParams.x/.y, creating an offset).
 *     b) ScaleType is set to FIT_START so the image starts at (0,0) of the view.
 *     c) The image resource itself has its tip at pixel (0,0) of the bitmap.
 *   Fix: set scaleType = FIT_START, padding = 0 explicitly.  Also, we now track a
 *   separate tapX/tapY that is offset by a small HOT_SPOT_OFFSET_PX constant (default 0)
 *   so it can be tuned if the cursor bitmap places its tip inset from the corner.
 *
 * BUG 4 – Missing Drag / Swipe Gestures:
 *   The original code only called injectTap() on LEFT button-down, so there was no way
 *   to model a press-hold-drag sequence.  The GestureDescription API supports this via
 *   StrokeDescription.continueStroke() — a chain of strokes where each one picks up
 *   where the previous left off.
 *
 *   State machine:
 *     LEFT DOWN  → begin a "continuing" stroke at current position.  Dispatch a 1ms
 *                  stroke with willContinue=true so the system starts a gesture but
 *                  waits for continuations.
 *     MOUSE MOVE (while LEFT held) → append a continuation stroke along the delta path.
 *     LEFT UP    → dispatch a final "terminating" stroke with willContinue=false.
 *
 *   This allows full drag-to-select, drag-and-drop, and navigation swipes.
 */
class MouseAccessibilityService : AccessibilityService() {

    private lateinit var windowManager: WindowManager
    private var cursorView: ImageView? = null
    private lateinit var layoutParams: WindowManager.LayoutParams

    private val serviceScope = CoroutineScope(Dispatchers.Main + SupervisorJob())

    // Screen dimensions (drawable area, excluding system bars)
    private var screenWidth = 0
    private var screenHeight = 0

    // Current virtual cursor position (top-left of cursor ImageView, in screen pixels)
    private var currentX = 0
    private var currentY = 0
    private var isCursorActive = false

    // ── Bug 4: Drag state ─────────────────────────────────────────────────────
    private var isLeftButtonDown = false
    private var activeDragStroke: GestureDescription.StrokeDescription? = null
    // Accumulate path points for the current drag segment
    private var dragPath: Path? = null
    private var dragLastX = 0f
    private var dragLastY = 0f
    // Running time offset within the current gesture (milliseconds)
    private var dragTimeOffset = 0L

    override fun onServiceConnected() {
        super.onServiceConnected()
        Log.i(TAG, "MouseAccessibilityService connected.")

        windowManager = getSystemService(WINDOW_SERVICE) as WindowManager

        // Use currentWindowMetrics on API 30+ for correct drawable-area dimensions
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

        currentX = 15
        currentY = screenHeight / 2

        initializeCursorOverlay()

        // ── Bug 2 Fix: send INIT packet so the Python host knows our real resolution ──
        // Dispatch after a short delay to ensure the TCP output stream is open on the
        // Android client side (InputBridgeService connects slightly after the service starts).
        serviceScope.launch {
            delay(800)
            InputEventBus.sendInitPacket(screenWidth, screenHeight)
            Log.i(TAG, "INIT:${screenWidth}:${screenHeight} sent to PC host.")
        }

        startInputEventCollection()
    }

    override fun onAccessibilityEvent(event: android.view.accessibility.AccessibilityEvent?) {
        // No-op: we only use this service for gesture injection and overlays.
    }

    override fun onInterrupt() {
        Log.w(TAG, "Accessibility Service interrupted.")
    }

    // ── Cursor overlay ────────────────────────────────────────────────────────

    private fun initializeCursorOverlay() {
        val view = ImageView(this).apply {
            setImageResource(R.drawable.ic_mouse_cursor)
            // ── Bug 3 Fix: zero padding + FIT_START so the image tip sits exactly
            //    at the view's (0,0), which matches layoutParams.x/.y and thus the
            //    coordinates passed to dispatchGesture().
            setPadding(0, 0, 0, 0)
            scaleType = ImageView.ScaleType.FIT_START
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
            gravity = Gravity.TOP or Gravity.START
            x = currentX
            y = currentY
        }

        try {
            windowManager.addView(view, layoutParams)
            cursorView = view
            Log.i(TAG, "Cursor overlay added at ($currentX, $currentY).")
        } catch (e: Exception) {
            Log.e(TAG, "Failed to add cursor view: ${e.message}", e)
        }
    }

    private fun showCursor() {
        val view = cursorView ?: return
        isCursorActive = true
        view.visibility = View.VISIBLE
        layoutParams.x = currentX
        layoutParams.y = currentY
        try {
            windowManager.updateViewLayout(view, layoutParams)
            Log.i(TAG, "Cursor shown at ($currentX, $currentY).")
        } catch (e: Exception) {
            Log.e(TAG, "Error showing cursor: ${e.message}")
        }
    }

    // ── Event collection ──────────────────────────────────────────────────────

    private fun startInputEventCollection() {
        serviceScope.launch {
            InputEventBus.events.collect { event ->
                when (event) {
                    is InputEvent.MouseMove   -> handleMouseMove(event.dx, event.dy)
                    is InputEvent.MouseClick  -> handleMouseClick(event.button, event.state)
                    is InputEvent.MouseScroll -> handleMouseScroll(event.dy)
                    else -> { /* Keystrokes handled by KeyboardInjectionService (IME) */ }
                }
            }
        }
    }

    // ── Input handlers ────────────────────────────────────────────────────────

    /**
     * Moves the cursor overlay and, if a drag is in progress, continues the active gesture.
     */
    private fun handleMouseMove(dx: Int, dy: Int) {
        if (!isCursorActive) {
            showCursor()
        }

        currentX = (currentX + dx).coerceIn(0, screenWidth - 1)
        currentY = (currentY + dy).coerceIn(0, screenHeight - 1)

        if (currentX <= 0) {
            // Cancel any active drag before unlocking
            cancelDrag()
            triggerEdgeUnlock()
            return
        }

        // Update cursor overlay position
        layoutParams.x = currentX
        layoutParams.y = currentY
        try {
            val view = cursorView
            if (view != null && view.parent != null) {
                windowManager.updateViewLayout(view, layoutParams)
            }
        } catch (e: Exception) {
            Log.e(TAG, "Error updating cursor layout: ${e.message}")
        }

        // ── Bug 4 Fix: if left button is held, continue the drag stroke ───────
        if (isLeftButtonDown) {
            continueDrag(currentX.toFloat(), currentY.toFloat())
        }
    }

    /**
     * Handles mouse button press and release.
     * LEFT DOWN  → begin drag stroke (willContinue = true).
     * LEFT UP    → terminate drag stroke (willContinue = false).
     * RIGHT DOWN → context-menu long-press simulation.
     */
    private fun handleMouseClick(button: String, state: Int) {
        if (!isCursorActive) return

        val x = currentX.toFloat() + HOT_SPOT_OFFSET_PX
        val y = currentY.toFloat() + HOT_SPOT_OFFSET_PX

        when {
            button == "LEFT" && state == 1 -> {
                // ── Bug 4 Fix: begin drag stroke ──────────────────────────────
                isLeftButtonDown = true
                beginDrag(x, y)
            }
            button == "LEFT" && state == 0 -> {
                // Button released – terminate the drag/tap stroke
                isLeftButtonDown = false
                endDrag(x, y)
            }
            button == "RIGHT" && state == 1 -> {
                // Simulate a long-press for context menus
                injectLongPress(x, y)
            }
        }
    }

    /**
     * Dispatches a scroll swipe gesture under the cursor.
     * dy > 0 = scroll down (content moves up); dy < 0 = scroll up.
     */
    private fun handleMouseScroll(dy: Int) {
        if (!isCursorActive) return
        val swipeDistance = dy.toFloat().coerceIn(-600f, 600f)
        injectSwipe(
            currentX.toFloat(), currentY.toFloat(),
            currentX.toFloat(), currentY - swipeDistance,
            durationMs = 180L
        )
    }

    // ── Drag state machine ────────────────────────────────────────────────────

    /**
     * Starts a new drag gesture at (x, y).
     * Uses willContinue=true so the system holds the gesture open for continuations.
     */
    private fun beginDrag(x: Float, y: Float) {
        dragLastX = x
        dragLastY = y
        dragTimeOffset = 0L

        val path = Path().apply {
            moveTo(x, y)
            lineTo(x + 1f, y)   // non-zero length required by some vendor gesture engines
        }

        // 1ms duration – just enough to register the press without moving
        val stroke = GestureDescription.StrokeDescription(path, 0L, 1L, true)
        activeDragStroke = stroke
        dragPath = path

        val gesture = GestureDescription.Builder().addStroke(stroke).build()
        dispatchGesture(gesture, object : GestureResultCallback() {
            override fun onCompleted(g: GestureDescription?) {
                Log.d(TAG, "Drag started at ($x, $y).")
            }
            override fun onCancelled(g: GestureDescription?) {
                Log.w(TAG, "Drag start cancelled at ($x, $y).")
                cancelDrag()
            }
        }, null)
    }

    /**
     * Appends a continuation stroke to the active drag gesture.
     * Called for every mouse-move event while the left button is held.
     */
    private fun continueDrag(toX: Float, toY: Float) {
        val prevStroke = activeDragStroke ?: return
        if (dragLastX == toX && dragLastY == toY) return   // no movement, skip

        dragTimeOffset += DRAG_SEGMENT_MS

        val path = Path().apply {
            moveTo(dragLastX, dragLastY)
            lineTo(toX, toY)
        }

        val continuation = prevStroke.continueStroke(path, dragTimeOffset, DRAG_SEGMENT_MS, true)
        activeDragStroke = continuation
        dragLastX = toX
        dragLastY = toY

        val gesture = GestureDescription.Builder().addStroke(continuation).build()
        dispatchGesture(gesture, object : GestureResultCallback() {
            override fun onCancelled(g: GestureDescription?) {
                Log.w(TAG, "Drag continuation cancelled – resetting drag state.")
                cancelDrag()
            }
        }, null)
    }

    /**
     * Terminates the drag gesture at (x, y) with willContinue=false.
     * If the button was released with zero total movement, this produces a clean tap.
     */
    private fun endDrag(x: Float, y: Float) {
        val prevStroke = activeDragStroke

        if (prevStroke == null) {
            // No active gesture (e.g. drag was cancelled) – inject a simple tap
            injectTap(x, y)
            return
        }

        dragTimeOffset += DRAG_SEGMENT_MS

        val path = Path().apply {
            moveTo(dragLastX, dragLastY)
            lineTo(x, y)
        }

        // willContinue = false → gesture ends here
        val terminator = prevStroke.continueStroke(path, dragTimeOffset, DRAG_SEGMENT_MS, false)
        activeDragStroke = null
        dragPath = null

        val gesture = GestureDescription.Builder().addStroke(terminator).build()
        dispatchGesture(gesture, object : GestureResultCallback() {
            override fun onCompleted(g: GestureDescription?) {
                Log.d(TAG, "Drag ended at ($x, $y).")
            }
            override fun onCancelled(g: GestureDescription?) {
                Log.w(TAG, "Drag end cancelled.")
            }
        }, null)
    }

    /** Clears all drag state without sending a termination gesture. */
    private fun cancelDrag() {
        activeDragStroke = null
        dragPath = null
        isLeftButtonDown = false
        Log.d(TAG, "Drag cancelled (state reset).")
    }

    // ── Unlock ────────────────────────────────────────────────────────────────

    private fun triggerEdgeUnlock() {
        isCursorActive = false
        cursorView?.visibility = View.GONE
        currentX = 15
        currentY = screenHeight / 2
        Log.i(TAG, "Cursor exited Android screen edge. Requesting PC unlock.")
        serviceScope.launch {
            InputEventBus.requestUnlock()
        }
    }

    // ── Gesture helpers ───────────────────────────────────────────────────────

    /**
     * Injects a single-point tap gesture.
     * Uses a 1-pixel lineTo for vendor gesture recogniser compatibility.
     */
    private fun injectTap(x: Float, y: Float) {
        val path = Path().apply {
            moveTo(x, y)
            lineTo(x + 1f, y + 1f)
        }
        val stroke = GestureDescription.StrokeDescription(path, 0L, 60L)
        val gesture = GestureDescription.Builder().addStroke(stroke).build()
        dispatchGesture(gesture, object : GestureResultCallback() {
            override fun onCompleted(g: GestureDescription?) { Log.d(TAG, "Tap at ($x, $y).") }
            override fun onCancelled(g: GestureDescription?) { Log.w(TAG, "Tap cancelled at ($x, $y).") }
        }, null)
    }

    /** Simulates a long-press (500 ms) for context menus / drag-to-reorder activation. */
    private fun injectLongPress(x: Float, y: Float) {
        val path = Path().apply { moveTo(x, y); lineTo(x + 1f, y) }
        val stroke = GestureDescription.StrokeDescription(path, 0L, 500L)
        val gesture = GestureDescription.Builder().addStroke(stroke).build()
        dispatchGesture(gesture, object : GestureResultCallback() {
            override fun onCompleted(g: GestureDescription?) { Log.d(TAG, "Long-press at ($x, $y).") }
        }, null)
    }

    private fun injectSwipe(
        startX: Float, startY: Float,
        endX: Float, endY: Float,
        durationMs: Long = 150L
    ) {
        val path = Path().apply { moveTo(startX, startY); lineTo(endX, endY) }
        val stroke = GestureDescription.StrokeDescription(path, 0L, durationMs)
        val gesture = GestureDescription.Builder().addStroke(stroke).build()
        dispatchGesture(gesture, object : GestureResultCallback() {
            override fun onCompleted(g: GestureDescription?) { Log.d(TAG, "Swipe ($startX,$startY)→($endX,$endY).") }
            override fun onCancelled(g: GestureDescription?) { Log.w(TAG, "Swipe cancelled.") }
        }, null)
    }

    // ── Lifecycle ─────────────────────────────────────────────────────────────

    override fun onDestroy() {
        Log.i(TAG, "MouseAccessibilityService destroyed.")
        val view = cursorView
        if (view != null) {
            try {
                if (view.parent != null) windowManager.removeView(view)
            } catch (e: Exception) {
                Log.e(TAG, "Error removing cursor view: ${e.message}")
            }
            cursorView = null
        }
        serviceScope.cancel()
        super.onDestroy()
    }

    companion object {
        private const val TAG = "MouseAccessibilityService"

        // ── Bug 3: hot-spot offset ────────────────────────────────────────────
        // 0 = the tap coordinate == the top-left pixel of the ImageView.
        // If the cursor bitmap has its visual tip inset from the corner (e.g. a
        // drop-shadow makes the actual tip land at pixel 2,2), set this to 2.
        private const val HOT_SPOT_OFFSET_PX = 0f

        // Duration of each drag segment stroke in milliseconds.
        // Lower = smoother drag but more gesture dispatches; 16ms ≈ 60fps.
        private const val DRAG_SEGMENT_MS = 16L
    }
}
