package com.deskstream.sync.event

import com.deskstream.sync.network.SocketClient

/**
 * Represent incoming events received over the network connection from the PC Host.
 */
sealed class InputEvent {
    data class MouseMove(val dx: Int, val dy: Int) : InputEvent()
    data class MouseClick(val button: String, val state: Int) : InputEvent()
    data class KeyText(val text: String) : InputEvent()
    data class KeyAction(val action: String) : InputEvent()
    data class ConnectionStateChanged(
        val state: SocketClient.ConnectionState,
        val error: String?
    ) : InputEvent()
}
