package com.deskstream.sync.service

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.graphics.Path
import android.graphics.PixelFormat
import android.util.Log
import android.view.Choreographer
import android.view.Gravity
import android.view.View
import android.view.ViewTreeObserver
import android.view.WindowManager
import android.widget.ImageView
import com.deskstream.sync.R
import com.deskstream.sync.event.InputEvent
import com.deskstream.sync.event.InputEventBus
import kotlinx.coroutines.*
import java.util.concurrent.atomic.AtomicInteger

/**
 * MouseAccessibilityService
 *
 * Renders a virtual cursor overlay and injects gestures via the Accessibility API.
 *
 * ── Fix log ───────────────────────────────────────────────────────────────────
 *
 * BUG 1 – Scroll acts as tap (PREVIOUS):
 *   One wheel tick sends dy=±1.  The old code built a 1px swipe over 180ms
 *   (velocity ≈ 5 px/s), which Android classifies as TAP, not FLING.
 *   Fix: multiply dy by SCROLL_SCALE_PX (120 px/tick) and use SCROLL_DURATION_MS
 *   (80 ms) so velocity ≈ 1500 px/s, which always clears the fling threshold.
 *
 * BUG 2 – Right-click hangs (stuck press) (PREVIOUS):
 *   injectLongPress used a single 500 ms non-continuing stroke.  On MIUI, if the
 *   system intercepts ACTION_DOWN for its own shortcut sheet, the gesture callback
 *   fires onCancelled() and ACTION_UP is never delivered, freezing the pressed UI.
 *   Fix: model right-click as a two-stroke continueStroke sequence.
 *
 * BUG 3 – Drag / swipe broken (PREVIOUS):
 *   Each mouse-move event called dispatchGesture() on a new continuation, but
 *   dispatchGesture() CANCELS any currently executing gesture and starts fresh.
 *   Fix: accumulate all drag points into a single Path and dispatch it as ONE
 *   gesture on LEFT UP (endDrag).
 *
 * BUG 4 – Dynamic click Y-offset (PREVIOUS):
 *   Fixed via GlobalLayoutListener + getLocationOnScreen().
 *
 * ── NEW RENDERING FIXES ──────────────────────────────────────────────────────
 *
 * RENDER BUG 1 – Jumpy/Laggy Cursor (THIS PATCH):
 *   Root cause: every TCP packet arriving on the IO thread caused a synchronous
 *   windowManager.updateViewLayout() call on the Main Thread via the SharedFlow
 *   collector.  At 200–400 packets/s this saturates the UI thread and causes
 *   Choreographer frame drops (Jank), producing the visual stutter.
 *   Fix: Target position is stored in a pair of AtomicIntegers (targetX, targetY)
 *   updated freely by the IO/collector thread.  A Choreographer.FrameCallback
 *   fires ONCE per vsync frame (60-120 Hz), reads the target, linearly interpolates
 *   the visual position, and calls updateViewLayout exactly once per display frame.
 *   This decouples network throughput from display frame pacing completely.
 *
 * RENDER BUG 2 – Slow-Motion System Swipes (THIS PATCH):
 *   Root cause: endDrag() computed its gesture duration as
 *   (totalMovement * DRAG_MS_PER_PX), which grew proportionally to how long the
 *   user physically dragged.  A 1.5-second slow drag produced a 1500ms gesture
 *   stroke, and Android stretches the system "Home" / "Recents" animation to fill
 *   the entire stroke duration, making it appear in extreme slow motion.
 *   Fix: the final gesture duration is now capped at MAX_SYSTEM_GESTURE_MS (350ms).
 *   This normalises swipe velocity so Android's system animations always play at
 *   their native speed regardless of the user's physical drag tempo.
 *
 * RENDER BUG 3 – Sensitivity/Control (THIS PATCH — Python host side):
 *   Sub-pixel accumulator and √-easing curve implemented in mouse_hook.py on the
 *   host.  The Android side receives already-smoothed integer deltas; no change here
 *   beyond the frame-decoupled rendering path (Render Bug 1 fix above) which makes
 *   the cursor feel smooth at any sensitivity level.
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
    @Volatile
    private var yInsetOffset = 0

    // ── RENDER BUG 1 FIX: Choreographer-decoupled cursor position ─────────
    // targetX / targetY are the authoritative logical position updated by the
    // network/collector thread via handleMouseMove().  They are AtomicIntegers
    // so cross-thread writes are safe without holding a lock on the UI thread.
    private val targetX = AtomicInteger(15)
    private val targetY = AtomicInteger(0)

    // Visual (interpolated) cursor position — only read/written on UI thread
    // inside the Choreographer callback.
    private var visualX = 15f
    private var visualY = 0f

    // Lerp factor: fraction of (target – visual) to close per frame.
    // 0.7 → ~3 frames to settle = ~50ms lag at 60 Hz, imperceptible but smooth.
    // Set to 1.0f to disable interpolation (instant snapping).
    private val LERP_FACTOR = 0.70f

    @Volatile
    private var isCursorActive = false
    private var isFrameCallbackScheduled = false

    // Current logical position (also used by gesture injection — gestures use
    // target position, not interpolated visual position, for accuracy).
    private var currentX: Int
        get() = targetX.get()
        set(v) = targetX.set(v)
    private var currentY: Int
        get() = targetY.get()
        set(v) = targetY.set(v)

    // ── BUG 3 FIX: path-accumulation drag state ───────────────────────────────
    private val dragLock = Any()
    private var isLeftButtonDown = false
    private var dragStartX = 0f
    private var dragStartY = 0f
    private var dragAccumPath: Path? = null
    private var dragLastX = 0f
    private var dragLastY = 0f
    private var anchorStroke: GestureDescription.StrokeDescription? = null

    // ── Choreographer frame callback ─────────────────────────────────────────
    private val frameCallback = object : Choreographer.FrameCallback {
        override fun doFrame(frameTimeNanos: Long) {
            val tx = targetX.get().toFloat()
            val ty = targetY.get().toFloat()

            // Linear interpolation toward target
            visualX += (tx - visualX) * LERP_FACTOR
            visualY += (ty - visualY) * LERP_FACTOR

            val newX = visualX.toInt()
            val newY = visualY.toInt()

            // Only issue a WM call if the rounded position actually changed
            if (newX != layoutParams.x || newY != layoutParams.y) {
                layoutParams.x = newX
                layoutParams.y = newY
                val view = cursorView
                if (view != null && view.parent != null) {
                    try {
                        windowManager.updateViewLayout(view, layoutParams)
                    } catch (e: Exception) {
                        Log.e(TAG, "Choreographer updateViewLayout error: ${e.message}")
                    }
                }
            }

            // Keep re-posting as long as cursor is active
            if (isCursorActive) {
                Choreographer.getInstance().postFrameCallback(this)
            } else {
                isFrameCallbackScheduled = false
            }
        }
    }

    /** Ensures the Choreographer loop is running. Call from Main thread only. */
    private fun ensureFrameCallbackRunning() {
        if (!isFrameCallbackScheduled) {
            isFrameCallbackScheduled = true
            Choreographer.getInstance().postFrameCallback(frameCallback)
        }
    }

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

        targetX.set(15)
        targetY.set(screenHeight / 2)
        visualX = 15f
        visualY = screenHeight / 2f

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
            x = targetX.get()
            y = targetY.get()
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
                    yInsetOffset = loc[1] - layoutParams.y
                    Log.i(TAG, "View placed at screen Y=${loc[1]}, layoutParams.y=${layoutParams.y}" +
                               ", yInsetOffset=$yInsetOffset px")
                    view.viewTreeObserver.removeOnGlobalLayoutListener(this)
                }
            })

            Log.i(TAG, "Cursor overlay added at (${targetX.get()}, ${targetY.get()}).")
        } catch (e: Exception) {
            Log.e(TAG, "Failed to add cursor view: ${e.message}", e)
        }
    }

    private fun showCursor() {
        val view = cursorView ?: return
        isCursorActive = true
        view.visibility = View.VISIBLE
        // Seed visual position to current logical position to avoid a lurch from (0,0)
        visualX = targetX.get().toFloat()
        visualY = targetY.get().toFloat()
        layoutParams.x = targetX.get()
        layoutParams.y = targetY.get()
        try {
            windowManager.updateViewLayout(view, layoutParams)
        } catch (e: Exception) {
            Log.e(TAG, "Error showing cursor: ${e.message}")
        }
        // ── RENDER BUG 1 FIX: start per-vsync Choreographer loop ─────────────
        ensureFrameCallbackRunning()
    }

    // ── Event collection ──────────────────────────────────────────────────────

    private fun startInputEventCollection() {
        serviceScope.launch(Dispatchers.Default) {
            InputEventBus.events.collect { event ->
                when (event) {
                    is InputEvent.MouseMove   -> handleMouseMove(event.dx, event.dy)
                    is InputEvent.MouseClick  -> handleMouseClick(event.button, event.state)
                    is InputEvent.MouseScroll -> handleMouseScroll(event.dy)
                    is InputEvent.ServiceStop -> {
                        // ── Bug 1 Fix: InputBridgeService has stopped — halt rendering ──
                        withContext(Dispatchers.Main) {
                            // 1. Remove the Choreographer callback immediately (not deferred).
                            //    Using removeFrameCallback() is safer than relying on the
                            //    isCursorActive flag, which only stops re-posting one frame later.
                            isCursorActive = false
                            isFrameCallbackScheduled = false
                            Choreographer.getInstance().removeFrameCallback(frameCallback)
                            // 2. Hide the cursor view. Do NOT call windowManager.removeView()
                            //    here — the OS will invoke it naturally when this
                            //    AccessibilityService is eventually destroyed.
                            cursorView?.visibility = View.GONE
                        }
                        Log.i(TAG, "ServiceStop received — cursor hidden, render loop paused.")
                    }
                    else -> {}
                }
            }
        }
    }

    // ── Input handlers ────────────────────────────────────────────────────────

    /**
     * RENDER BUG 1 FIX:
     * handleMouseMove() now only UPDATES the AtomicInteger target position.
     * It does NOT call windowManager.updateViewLayout() itself.
     * The Choreographer FrameCallback (running at display vsync rate) does the
     * actual WM call, at most once per frame, with smooth interpolation.
     */
    private fun handleMouseMove(dx: Int, dy: Int) {
        if (!isCursorActive) {
            // Switch to Main thread for showCursor() since it calls WM APIs
            serviceScope.launch { showCursor() }
        }

        val newX = (targetX.get() + dx).coerceIn(0, screenWidth - 1)
        val newY = (targetY.get() + dy).coerceIn(0, screenHeight - 1)
        targetX.set(newX)
        targetY.set(newY)

        if (newX <= 0) {
            cancelDrag()
            serviceScope.launch { triggerEdgeUnlock() }
            return
        }

        // ── BUG 3 FIX: accumulate into dragAccumPath; do NOT dispatch yet ────
        synchronized(dragLock) {
            if (isLeftButtonDown) {
                val gx = newX.toFloat()
                val gy = gestureY(newY.toFloat())
                dragAccumPath?.lineTo(gx, gy)
                dragLastX = gx
                dragLastY = gy
            }
        }
    }

    /**
     * BUG 4 FIX – returns the absolute Y coordinate for dispatchGesture() calls.
     */
    private fun gestureY(layoutRelativeY: Float): Float = layoutRelativeY + yInsetOffset

    /**
     * LEFT DOWN  → record start, build accumulation path, dispatch anchor hold stroke.
     * LEFT UP    → dispatch full accumulated path as one gesture (= clean drag or tap).
     * RIGHT      → two-stroke long-press (DOWN willContinue=true, then UP).
     */
    private fun handleMouseClick(button: String, state: Int) {
        if (!isCursorActive) return

        val gx = targetX.get().toFloat() + HOT_SPOT_OFFSET_PX
        val gy = gestureY(targetY.get().toFloat() + HOT_SPOT_OFFSET_PX)

        when {
            button == "LEFT" && state == 1 -> {
                synchronized(dragLock) { isLeftButtonDown = true }
                beginDrag(gx, gy)
            }
            button == "LEFT" && state == 0 -> {
                synchronized(dragLock) { isLeftButtonDown = false }
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
     */
    private fun handleMouseScroll(dy: Int) {
        if (!isCursorActive) return
        val startX = targetX.get().toFloat()
        val startY = gestureY(targetY.get().toFloat())
        val endY = startY - (dy * SCROLL_SCALE_PX)
        injectSwipe(startX, startY, startX, endY, durationMs = SCROLL_DURATION_MS)
    }

    // ── Drag state machine (path-accumulation model) ──────────────────────────

    /**
     * BUG 3 FIX – begin drag:
     * Dispatch a 1 ms anchor stroke (willContinue=true) to register the DOWN event.
     */
    private fun beginDrag(x: Float, y: Float) {
        synchronized(dragLock) {
            dragStartX = x
            dragStartY = y
            dragLastX = x
            dragLastY = y

            dragAccumPath = Path().apply { moveTo(x, y) }

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
    }

    /**
     * RENDER BUG 2 FIX – end drag:
     *
     * Previous behaviour: durationMs = (totalMovement * DRAG_MS_PER_PX), uncapped.
     * For a slow 1.5-second physical drag over ~600 virtual pixels this produced a
     * 900 ms gesture.  Android's system-gesture recogniser (Home, Recents) stretches
     * its animation to fill the full gesture duration, causing the slow-motion effect.
     *
     * Fix: cap the gesture duration at MAX_SYSTEM_GESTURE_MS (350 ms).
     * This normalises the injected swipe velocity so system animations always play
     * at their native snappy speed, regardless of how slowly the user dragged.
     *
     * The lower bound (80 ms) is kept so micro-taps are not classified as flings.
     */
    private fun endDrag(x: Float, y: Float) {
        synchronized(dragLock) {
            val path = dragAccumPath

            val dx = x - dragStartX
            val dy = y - dragStartY
            val totalMovement = Math.hypot(dx.toDouble(), dy.toDouble()).toFloat()

            dragAccumPath = null
            anchorStroke = null

            if (path == null || totalMovement < DRAG_MIN_MOVEMENT_PX) {
                injectTap(x, y)
                return
            }

            path.lineTo(x, y)

            // ── RENDER BUG 2 FIX: cap to MAX_SYSTEM_GESTURE_MS so Android's system
            // gesture recogniser (Home/Recents swipe) plays at native speed. ────────
            val durationMs = (totalMovement * DRAG_MS_PER_PX)
                .toLong()
                .coerceIn(80L, MAX_SYSTEM_GESTURE_MS)

            val stroke = GestureDescription.StrokeDescription(path, 0L, durationMs, false)
            val gesture = GestureDescription.Builder().addStroke(stroke).build()
            dispatchGesture(gesture, object : GestureResultCallback() {
                override fun onCompleted(g: GestureDescription?) { Log.d(TAG, "Drag ended at ($x, $y) in ${durationMs}ms.") }
                override fun onCancelled(g: GestureDescription?) { Log.w(TAG, "Drag end cancelled.") }
            }, null)
        }
    }

    private fun cancelDrag() {
        synchronized(dragLock) {
            dragAccumPath = null
            anchorStroke = null
            isLeftButtonDown = false
            dragStartX = 0f; dragStartY = 0f
            dragLastX = 0f; dragLastY = 0f
            Log.d(TAG, "Drag cancelled (state reset).")
        }
    }

    // ── Unlock ────────────────────────────────────────────────────────────────

    private fun triggerEdgeUnlock() {
        // Stop Choreographer loop
        isCursorActive = false
        isFrameCallbackScheduled = false
        cursorView?.visibility = View.GONE
        targetX.set(15)
        targetY.set(screenHeight / 2)
        visualX = 15f
        visualY = screenHeight / 2f
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
     * Two-stroke continueStroke sequence guarantees ACTION_UP is always delivered.
     */
    private fun injectRightClick(x: Float, y: Float) {
        val downPath = Path().apply { moveTo(x, y); lineTo(x + 0.1f, y) }
        val downStroke = GestureDescription.StrokeDescription(downPath, 0L, 1L, true)

        val holdPath = Path().apply { moveTo(x, y); lineTo(x + 0.1f, y) }
        val holdStroke = downStroke.continueStroke(holdPath, 1L, LONG_PRESS_MS, false)

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
        isCursorActive = false
        isFrameCallbackScheduled = false
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

        // ── BUG 1 (original): scroll constants ───────────────────────────────
        private const val SCROLL_SCALE_PX = 120f
        private const val SCROLL_DURATION_MS = 80L

        // ── BUG 2 (original): right-click long-press hold duration ────────────
        private const val LONG_PRESS_MS = 500L

        // ── BUG 3 (original): drag constants ─────────────────────────────────
        private const val DRAG_ANCHOR_MS = 1L
        private const val DRAG_MIN_MOVEMENT_PX = 8f
        private const val DRAG_MS_PER_PX = 1.5f

        // ── RENDER BUG 2 FIX: max gesture duration cap ───────────────────────
        // System swipe gestures (Home, Recents) stretch their animations to fill
        // the gesture duration.  Capping at 350ms forces a snappy native speed.
        private const val MAX_SYSTEM_GESTURE_MS = 350L
    }
}
