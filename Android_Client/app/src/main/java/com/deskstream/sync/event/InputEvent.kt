package com.deskstream.sync.event

import com.deskstream.sync.network.SocketClient

/**
 * Represent incoming events received over the network connection from the PC Host.
 */
sealed class InputEvent {
    data class MouseMove(val dx: Int, val dy: Int) : InputEvent()
    data class MouseClick(val button: String, val state: Int) : InputEvent()
    /** Vertical scroll tick from the PC host. dy > 0 = scroll down. */
    data class MouseScroll(val dy: Int) : InputEvent()
    data class KeyText(val text: String) : InputEvent()
    data class KeyAction(val action: String) : InputEvent()
    data class ConnectionStateChanged(
        val state: SocketClient.ConnectionState,
        val error: String?
    ) : InputEvent()

    /**
     * Emitted by [com.deskstream.sync.service.InputBridgeService] during [onDestroy] to
     * signal all UI consumers (e.g. MouseAccessibilityService) that the network bridge
     * has stopped. Consumers should hide/pause any active rendering or input loops.
     */
    object ServiceStop : InputEvent()
}
