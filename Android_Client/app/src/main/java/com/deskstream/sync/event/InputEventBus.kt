package com.deskstream.sync.event

import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.SharedFlow
import kotlinx.coroutines.flow.asSharedFlow

/**
 * Thread-safe central event broker that allows background networking tasks to emit KVM events,
 * and active OS integrations (AccessibilityService and InputMethodService) to consume them.
 */
object InputEventBus {
    private val _events = MutableSharedFlow<InputEvent>(extraBufferCapacity = 128)
    val events: SharedFlow<InputEvent> = _events.asSharedFlow()

    private val _unlockEvents = MutableSharedFlow<Unit>(extraBufferCapacity = 8)
    val unlockEvents: SharedFlow<Unit> = _unlockEvents.asSharedFlow()

    /** Emitted by MouseAccessibilityService once screen dimensions are known. */
    private val _initEvents = MutableSharedFlow<Pair<Int,Int>>(extraBufferCapacity = 4)
    val initEvents: SharedFlow<Pair<Int,Int>> = _initEvents.asSharedFlow()

    /**
     * Emits a new KVM input event received from the PC host.
     */
    suspend fun emit(event: InputEvent) {
        _events.emit(event)
    }

    /**
     * Non-blocking attempt to emit a new KVM input event.
     */
    fun tryEmit(event: InputEvent): Boolean {
        return _events.tryEmit(event)
    }

    /**
     * Requests the socket client to send an UNLOCK request back to the PC.
     * Triggers when the Android cursor exits the screen bounds.
     */
    suspend fun requestUnlock() {
        _unlockEvents.emit(Unit)
    }

    /**
     * Non-blocking attempt to request an UNLOCK sequence.
     */
    fun tryRequestUnlock(): Boolean {
        return _unlockEvents.tryEmit(Unit)
    }

    /**
     * Called by MouseAccessibilityService after onServiceConnected() to notify the
     * PC host of the real device screen dimensions (INIT handshake).
     */
    suspend fun sendInitPacket(width: Int, height: Int) {
        _initEvents.emit(Pair(width, height))
    }
}
