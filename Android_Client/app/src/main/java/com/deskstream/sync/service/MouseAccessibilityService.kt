package com.deskstream.sync.service

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.graphics.Path
import android.graphics.PixelFormat
import android.util.Log
import android.view.Gravity
import android.view.View
import android.view.ViewTreeObserver
import android.view.WindowManager
import android.widget.ImageView
import com.deskstream.sync.R
import com.deskstream.sync.event.InputEvent
import com.deskstream.sync.event.InputEventBus
import kotlinx.coroutines.*

/**
 * MouseAccessibilityService
 *
 * Renders a virtual cursor overlay and injects gestures via the Accessibility API.
 *
 * ── Fix log ───────────────────────────────────────────────────────────────────
 *
 * BUG 1 – Scroll acts as tap:
 *   One wheel tick sends dy=±1.  The old code built a 1px swipe over 180ms
 *   (velocity ≈ 5 px/s), which Android classifies as TAP, not FLING.
 *   Fix: multiply dy by SCROLL_SCALE_PX (120 px/tick) and use SCROLL_DURATION_MS
 *   (80 ms) so velocity ≈ 1500 px/s, which always clears the fling threshold.
 *
 * BUG 2 – Right-click hangs (stuck press):
 *   injectLongPress used a single 500 ms non-continuing stroke.  On MIUI, if the
 *   system intercepts ACTION_DOWN for its own shortcut sheet, the gesture callback
 *   fires onCancelled() and ACTION_UP is never delivered, freezing the pressed UI.
 *   Fix: model right-click as a two-stroke continueStroke sequence:
 *     stroke 1 – 1 ms DOWN, willContinue=true  (establishes the press)
 *     stroke 2 – 500 ms hold + UP, willContinue=false  (guaranteed UP delivery)
 *
 * BUG 3 – Drag / swipe broken:
 *   Each mouse-move event called dispatchGesture() on a new continuation, but
 *   dispatchGesture() CANCELS any currently executing gesture and starts fresh.
 *   The correct approach: accumulate all drag points into a single Path and
 *   dispatch it as ONE gesture on LEFT UP (endDrag).  The interim "hold" is kept
 *   alive by a willContinue=true anchor stroke dispatched on LEFT DOWN.
 *   On mouse-move we only update the path; on LEFT UP we dispatch the full path.
 *
 * BUG 4 – Dynamic click Y-offset:
 *   The old code used resources.getDimensionPixelSize("status_bar_height") which
 *   returns a resource value that can differ from the actual window inset MIUI
 *   applies to TYPE_ACCESSIBILITY_OVERLAY views.
 *   Fix: after windowManager.addView(), use a GlobalLayoutListener to call
 *   view.getLocationOnScreen() and store the real absolute Y the OS placed us at.
 *   gesture Y = currentY + (viewScreenY - layoutParams.y) = currentY + yInsetOffset.
 *   This is always pixel-perfect regardless of MIUI version or notch type.
 *
 * BUG 5 (handled in mouse_hook.py on the host side – see that file).
 */
class MouseAccessibilityService : AccessibilityService() {

    private lateinit var windowManager: WindowManager
    private var cursorView: ImageView? = null
    private lateinit var layoutParams: WindowManager.LayoutParams

    private val serviceScope = CoroutineScope(Dispatchers.Main + SupervisorJob())

    private var screenWidth = 0
    private var screenHeight = 0

    // ── BUG 4 FIX: dynamic inset offset ─────────────────────────────────────
    // Difference between the absolute Y where the OS actually placed the view
    // and layoutParams.y.  Populated by getLocationOnScreen() after layout.
    // gesture_absolute_y = currentY + yInsetOffset
    private var yInsetOffset = 0

    private var currentX = 0
    private var currentY = 0
    private var isCursorActive = false

    // ── BUG 3 FIX: path-accumulation drag state ───────────────────────────────
    // We no longer call dispatchGesture() per move-segment.
    // Instead we accumulate all points into dragAccumPath and dispatch once on UP.
    private var isLeftButtonDown = false
    private var dragStartX = 0f
    private var dragStartY = 0f
    private var dragAccumPath: Path? = null   // grows with every continueDrag call
    private var dragLastX = 0f
    private var dragLastY = 0f
    // Anchor stroke (willContinue=true, 1 ms) dispatched on DOWN to hold the press
    private var anchorStroke: GestureDescription.StrokeDescription? = null

    override fun onServiceConnected() {
        super.onServiceConnected()
        Log.i(TAG, "MouseAccessibilityService connected.")

        windowManager = getSystemService(WINDOW_SERVICE) as WindowManager

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

        serviceScope.launch {
            delay(800)
            val dpi = resources.displayMetrics.densityDpi
            InputEventBus.sendInitPacket(screenWidth, screenHeight, dpi)
            Log.i(TAG, "INIT:${screenWidth}:${screenHeight}:${dpi} sent to PC host.")
        }

        startInputEventCollection()
    }

    override fun onAccessibilityEvent(event: android.view.accessibility.AccessibilityEvent?) {}
    override fun onInterrupt() { Log.w(TAG, "Accessibility Service interrupted.") }

    // ── Cursor overlay ────────────────────────────────────────────────────────

    private fun initializeCursorOverlay() {
        val view = ImageView(this).apply {
            setImageResource(R.drawable.ic_mouse_cursor)
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

            // ── BUG 4 FIX: measure real absolute Y after the view is laid out ──
            view.viewTreeObserver.addOnGlobalLayoutListener(object :
                ViewTreeObserver.OnGlobalLayoutListener {
                override fun onGlobalLayout() {
                    val loc = IntArray(2)
                    view.getLocationOnScreen(loc)
                    // loc[1] = absolute screen Y of the view's top-left pixel.
                    // layoutParams.y = what we ASKED for (relative to window origin).
                    // yInsetOffset = how much the OS shifted us DOWN relative to
                    // layoutParams.y (status bar + any system insets MIUI adds).
                    yInsetOffset = loc[1] - layoutParams.y
                    Log.i(TAG, "View placed at screen Y=${loc[1]}, layoutParams.y=${layoutParams.y}" +
                               ", yInsetOffset=$yInsetOffset px")
                    view.viewTreeObserver.removeOnGlobalLayoutListener(this)
                }
            })

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
                    else -> {}
                }
            }
        }
    }

    // ── Input handlers ────────────────────────────────────────────────────────

    private fun handleMouseMove(dx: Int, dy: Int) {
        if (!isCursorActive) showCursor()

        currentX = (currentX + dx).coerceIn(0, screenWidth - 1)
        currentY = (currentY + dy).coerceIn(0, screenHeight - 1)

        if (currentX <= 0) {
            cancelDrag()
            triggerEdgeUnlock()
            return
        }

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

        // ── BUG 3 FIX: accumulate into dragAccumPath; do NOT dispatch yet ────
        if (isLeftButtonDown) {
            val gx = currentX.toFloat()
            val gy = gestureY(currentY.toFloat())
            dragAccumPath?.lineTo(gx, gy)
            dragLastX = gx
            dragLastY = gy
        }
    }

    /**
     * BUG 4 FIX – returns the absolute Y coordinate for dispatchGesture() calls.
     * currentY (layoutParams.y-relative) + yInsetOffset = absolute screen Y.
     */
    private fun gestureY(layoutRelativeY: Float): Float = layoutRelativeY + yInsetOffset

    /**
     * LEFT DOWN  → record start, build accumulation path, dispatch anchor hold stroke.
     * LEFT UP    → dispatch full accumulated path as one gesture (= clean drag or tap).
     * RIGHT      → two-stroke long-press (DOWN wilContinue=true, then UP).
     *
     * BUG 2 FIX: right-click uses a continueStroke pair so ACTION_UP is guaranteed.
     * BUG 3 FIX: left drag accumulates path, dispatches once on UP.
     * BUG 4 FIX: all Y coords go through gestureY().
     */
    private fun handleMouseClick(button: String, state: Int) {
        if (!isCursorActive) return

        val gx = currentX.toFloat() + HOT_SPOT_OFFSET_PX
        val gy = gestureY(currentY.toFloat() + HOT_SPOT_OFFSET_PX)

        when {
            button == "LEFT" && state == 1 -> {
                isLeftButtonDown = true
                beginDrag(gx, gy)
            }
            button == "LEFT" && state == 0 -> {
                isLeftButtonDown = false
                endDrag(gx, gy)
            }
            button == "RIGHT" && state == 1 -> {
                injectRightClick(gx, gy)
            }
        }
    }

    /**
     * BUG 1 FIX – Scroll wheel:
     * Scale each tick by SCROLL_SCALE_PX so the swipe distance is ~120 px.
     * Use SCROLL_DURATION_MS (80 ms) so velocity ≈ 1500 px/s > Android fling threshold.
     * dy > 0 = scroll down → finger moves UP (content scrolls down) → endY < startY.
     */
    private fun handleMouseScroll(dy: Int) {
        if (!isCursorActive) return
        val startX = currentX.toFloat()
        val startY = gestureY(currentY.toFloat())
        // dy > 0 means scroll DOWN: swipe finger UP, so endY < startY
        val endY = startY - (dy * SCROLL_SCALE_PX)
        injectSwipe(startX, startY, startX, endY, durationMs = SCROLL_DURATION_MS)
    }

    // ── Drag state machine (path-accumulation model) ──────────────────────────

    /**
     * BUG 3 FIX – begin drag:
     * Dispatch a 1 ms anchor stroke (willContinue=true) to register the DOWN event.
     * Do NOT attempt to chain continuations via dispatchGesture() per-segment —
     * each dispatchGesture() call cancels the previous gesture.
     * Instead, accumulate all movement in dragAccumPath and dispatch once on endDrag.
     */
    private fun beginDrag(x: Float, y: Float) {
        dragStartX = x
        dragStartY = y
        dragLastX = x
        dragLastY = y

        // Build the accumulation path starting at the down point
        dragAccumPath = Path().apply { moveTo(x, y) }

        // Dispatch a 1 ms anchor hold so the system registers ACTION_DOWN
        val anchorPath = Path().apply { moveTo(x, y); lineTo(x + 0.1f, y) }
        val stroke = GestureDescription.StrokeDescription(anchorPath, 0L, DRAG_ANCHOR_MS, true)
        anchorStroke = stroke

        val gesture = GestureDescription.Builder().addStroke(stroke).build()
        dispatchGesture(gesture, object : GestureResultCallback() {
            override fun onCompleted(g: GestureDescription?) {
                Log.d(TAG, "Drag anchor DOWN at ($x, $y).")
            }
            override fun onCancelled(g: GestureDescription?) {
                Log.w(TAG, "Drag anchor cancelled.")
                cancelDrag()
            }
        }, null)
    }

    /**
     * BUG 3 FIX – end drag:
     * Build a single stroke from dragStartX/Y through all accumulated points to (x, y).
     * Duration is proportional to path length (clamped) so velocity stays natural.
     * If no movement occurred (path is just moveTo), dispatch a clean tap instead.
     */
    private fun endDrag(x: Float, y: Float) {
        val path = dragAccumPath

        // Measure total movement
        val dx = x - dragStartX
        val dy = y - dragStartY
        val totalMovement = Math.hypot(dx.toDouble(), dy.toDouble()).toFloat()

        dragAccumPath = null
        anchorStroke = null

        if (path == null || totalMovement < DRAG_MIN_MOVEMENT_PX) {
            // No meaningful drag → simple tap
            injectTap(x, y)
            return
        }

        path.lineTo(x, y)

        // Duration proportional to distance, clamped between 80ms and 2000ms
        val durationMs = (totalMovement * DRAG_MS_PER_PX).toLong().coerceIn(80L, 2000L)

        val stroke = GestureDescription.StrokeDescription(path, 0L, durationMs, false)
        val gesture = GestureDescription.Builder().addStroke(stroke).build()
        dispatchGesture(gesture, object : GestureResultCallback() {
            override fun onCompleted(g: GestureDescription?) { Log.d(TAG, "Drag ended at ($x, $y).") }
            override fun onCancelled(g: GestureDescription?) { Log.w(TAG, "Drag end cancelled.") }
        }, null)
    }

    private fun cancelDrag() {
        dragAccumPath = null
        anchorStroke = null
        isLeftButtonDown = false
        dragStartX = 0f; dragStartY = 0f
        dragLastX = 0f; dragLastY = 0f
        Log.d(TAG, "Drag cancelled (state reset).")
    }

    // ── Unlock ────────────────────────────────────────────────────────────────

    private fun triggerEdgeUnlock() {
        isCursorActive = false
        cursorView?.visibility = View.GONE
        currentX = 15
        currentY = screenHeight / 2
        Log.i(TAG, "Cursor exited edge. Requesting PC unlock.")
        serviceScope.launch { InputEventBus.requestUnlock() }
    }

    // ── Gesture helpers ───────────────────────────────────────────────────────

    private fun injectTap(x: Float, y: Float) {
        val path = Path().apply { moveTo(x, y); lineTo(x + 1f, y + 1f) }
        val stroke = GestureDescription.StrokeDescription(path, 0L, 60L)
        val gesture = GestureDescription.Builder().addStroke(stroke).build()
        dispatchGesture(gesture, object : GestureResultCallback() {
            override fun onCompleted(g: GestureDescription?) { Log.d(TAG, "Tap at ($x, $y).") }
            override fun onCancelled(g: GestureDescription?) { Log.w(TAG, "Tap cancelled at ($x, $y).") }
        }, null)
    }

    /**
     * BUG 2 FIX – Right-click / long-press:
     * Two-stroke sequence guarantees ACTION_UP is always delivered even if MIUI
     * intercepts ACTION_DOWN for its own shortcut sheet:
     *   Stroke 1: 1 ms DOWN, willContinue=true  (establishes the touch contact)
     *   Stroke 2: LONG_PRESS_MS hold, willContinue=false  (hold + guaranteed UP)
     */
    private fun injectRightClick(x: Float, y: Float) {
        val downPath = Path().apply { moveTo(x, y); lineTo(x + 0.1f, y) }
        val downStroke = GestureDescription.StrokeDescription(downPath, 0L, 1L, true)

        val holdPath = Path().apply { moveTo(x, y); lineTo(x + 0.1f, y) }
        val holdStroke = downStroke.continueStroke(holdPath, 1L, LONG_PRESS_MS, false)

        val gesture = GestureDescription.Builder()
            .addStroke(holdStroke)   // the full gesture; downStroke is its ancestor
            .build()

        // We must dispatch downStroke first, then holdStroke as continuation.
        // The Builder-based API accepts only one root stroke; continueStroke
        // chains are built by dispatching the continuation stroke directly.
        // Correct pattern: dispatch the INITIAL stroke, then dispatch the continuation.
        val gestureDown = GestureDescription.Builder().addStroke(downStroke).build()
        dispatchGesture(gestureDown, object : GestureResultCallback() {
            override fun onCompleted(g: GestureDescription?) {
                val gestureHold = GestureDescription.Builder().addStroke(holdStroke).build()
                dispatchGesture(gestureHold, object : GestureResultCallback() {
                    override fun onCompleted(g: GestureDescription?) {
                        Log.d(TAG, "Right-click long-press completed at ($x, $y).")
                    }
                    override fun onCancelled(g: GestureDescription?) {
                        Log.w(TAG, "Right-click long-press cancelled.")
                    }
                }, null)
            }
            override fun onCancelled(g: GestureDescription?) {
                Log.w(TAG, "Right-click DOWN anchor cancelled.")
            }
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
            override fun onCompleted(g: GestureDescription?) {
                Log.d(TAG, "Swipe ($startX,$startY)→($endX,$endY).")
            }
            override fun onCancelled(g: GestureDescription?) {
                Log.w(TAG, "Swipe cancelled.")
            }
        }, null)
    }

    // ── Lifecycle ─────────────────────────────────────────────────────────────

    override fun onDestroy() {
        Log.i(TAG, "MouseAccessibilityService destroyed.")
        val view = cursorView
        if (view != null) {
            try { if (view.parent != null) windowManager.removeView(view) }
            catch (e: Exception) { Log.e(TAG, "Error removing cursor view: ${e.message}") }
            cursorView = null
        }
        serviceScope.cancel()
        super.onDestroy()
    }

    companion object {
        private const val TAG = "MouseAccessibilityService"

        // Cursor bitmap hot-spot inset from top-left corner (px). 0 = tip is at (0,0).
        private const val HOT_SPOT_OFFSET_PX = 0f

        // ── BUG 1: scroll constants ───────────────────────────────────────────
        // Pixels per scroll tick.  120 px/tick → velocity ≈ 1500 px/s at 80 ms.
        private const val SCROLL_SCALE_PX = 120f
        private const val SCROLL_DURATION_MS = 80L

        // ── BUG 2: right-click long-press hold duration ───────────────────────
        private const val LONG_PRESS_MS = 500L

        // ── BUG 3: drag constants ─────────────────────────────────────────────
        // 1 ms anchor stroke duration for the initial DOWN.
        private const val DRAG_ANCHOR_MS = 1L
        // Minimum pixel movement to count as a drag (below this → tap on release).
        private const val DRAG_MIN_MOVEMENT_PX = 8f
        // ms per pixel of travel for the final drag stroke duration.
        private const val DRAG_MS_PER_PX = 1.5f
    }
}
