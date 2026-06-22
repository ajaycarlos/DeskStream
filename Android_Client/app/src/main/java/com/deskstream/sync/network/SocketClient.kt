package com.deskstream.sync.network

import android.util.Log
import kotlinx.coroutines.*
import java.io.BufferedReader
import java.io.InputStreamReader
import java.io.OutputStream
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.net.Socket
import java.util.concurrent.atomic.AtomicBoolean

/**
 * SocketClient manages the low-level network connection to the DeskStream Sync Python Host.
 * It establishes a reliable TCP connection for keyboard input and control commands, and
 * a low-latency UDP socket for real-time mouse coordinate streaming in Wi-Fi mode.
 * Contains automatic recovery, connection state dispatching, and security verification.
 */
class SocketClient(
    private val connectionMode: ConnectionMode,
    private val hostIp: String,
    private val port: Int = 8080
) {
    enum class ConnectionMode {
        USB, WIFI
    }

    enum class ConnectionState {
        DISCONNECTED,
        CONNECTING,
        CONNECTED
    }

    // Callback handlers to expose events to the holding service
    var onMouseMoveReceived: ((dx: Int, dy: Int) -> Unit)? = null
    var onMouseClickReceived: ((button: String, state: Int) -> Unit)? = null
    /** Invoked when a scroll-delta packet arrives (dy > 0 = scroll down). */
    var onMouseScrollReceived: ((dy: Int) -> Unit)? = null
    var onKeyTextReceived: ((text: String) -> Unit)? = null
    var onKeyActionReceived: ((action: String) -> Unit)? = null
    var onConnectionStateChanged: ((state: ConnectionState, error: String?) -> Unit)? = null

    private val isRunning = AtomicBoolean(false)
    private val clientScope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    
    private var tcpSocket: Socket? = null
    private var tcpOutputStream: OutputStream? = null
    private var udpSocket: DatagramSocket? = null
    
    private var connectionState = ConnectionState.DISCONNECTED
        set(value) {
            if (field != value) {
                field = value
                onConnectionStateChanged?.invoke(value, lastError)
            }
        }
    
    private var lastError: String? = null
    
    /**
     * Start the connection and polling loop on a background coroutine context.
     */
    fun start() {
        if (isRunning.compareAndSet(false, true)) {
            Log.i(TAG, "Starting SocketClient (Mode: $connectionMode, Host IP: $hostIp, Port: $port)")
            clientScope.launch {
                connectionLoop()
            }
        }
    }

    /**
     * Safely stop the connection and cancel all running coroutines.
     */
    fun stop() {
        if (isRunning.compareAndSet(true, false)) {
            Log.i(TAG, "Stopping SocketClient")
            cleanupSockets()
            clientScope.cancel()
        }
    }
    
    /**
     * Core connection manager loop. Handles network recoveries with exponential backoff.
     */
    private suspend fun connectionLoop() {
        var backoffMs = 1000L
        val maxBackoffMs = 10000L
        
        while (isRunning.get()) {
            try {
                connectionState = ConnectionState.CONNECTING
                lastError = null
                
                // 1. Determine local host route target
                // In USB Mode, ADB reverse port forwarding maps requests to 127.0.0.1
                val targetIp = if (connectionMode == ConnectionMode.USB) "127.0.0.1" else hostIp
                Log.d(TAG, "Connecting to TCP host at $targetIp:$port")
                
                val socket = Socket(targetIp, port)
                socket.soTimeout = 15000 // 15-second read timeout for keep-alive validation
                
                tcpSocket = socket
                tcpOutputStream = socket.getOutputStream()
                
                // 2. Wi-Fi mode streams mouse updates via UDP (port + 1)
                if (connectionMode == ConnectionMode.WIFI) {
                    val udpPort = port + 1
                    Log.d(TAG, "Binding UDP client listener to port $udpPort")
                    val datagramSocket = DatagramSocket(udpPort)
                    udpSocket = datagramSocket
                    startUdpListener(datagramSocket)
                }
                
                connectionState = ConnectionState.CONNECTED
                backoffMs = 1000L // Reset backoff on successful connection
                
                // 3. Keep reading the blocking TCP stream
                readTcpStream(socket)
                
            } catch (e: Exception) {
                if (isRunning.get()) {
                    lastError = e.message ?: e.toString()
                    Log.e(TAG, "Connection error: $lastError. Reconnecting in ${backoffMs / 1000}s...", e)
                    connectionState = ConnectionState.DISCONNECTED
                    cleanupSockets()
                    delay(backoffMs)
                    backoffMs = (backoffMs * 2).coerceAtMost(maxBackoffMs)
                }
            }
        }
    }
    
    /**
     * Reads lines from the TCP stream and processes them.
     */
    private fun readTcpStream(socket: Socket) {
        val reader = BufferedReader(InputStreamReader(socket.getInputStream(), "UTF-8"))
        while (isRunning.get() && !socket.isClosed) {
            val line = reader.readLine() ?: break // null indicates connection EOF (socket closed on host side)
            parseAndDispatch(line)
        }
    }
    
    /**
     * Reads incoming UDP packets for low-latency mouse actions (Wi-Fi Mode).
     */
    private fun startUdpListener(socket: DatagramSocket) {
        clientScope.launch(Dispatchers.IO) {
            val buffer = ByteArray(1024)
            val packet = DatagramPacket(buffer, buffer.size)
            
            while (isRunning.get() && !socket.isClosed) {
                try {
                    socket.receive(packet)
                    
                    // CYBERSECURITY: Explicit IP Device Pinning
                    // Verifies packet sender IP matches whitelist configuration to prevent spoofing
                    val senderIp = packet.address?.hostAddress
                    val allowedIp = if (connectionMode == ConnectionMode.USB) "127.0.0.1" else hostIp
                    
                    if (senderIp != allowedIp) {
                        Log.w(TAG, "Security Warning: Ignored UDP packet from unverified host: $senderIp (Expected: $allowedIp)")
                        continue
                    }
                    
                    val data = String(packet.data, packet.offset, packet.length, Charsets.UTF_8).trim()
                    if (data.isNotEmpty()) {
                        parseAndDispatch(data)
                    }
                } catch (e: Exception) {
                    if (isRunning.get() && !socket.isClosed) {
                        Log.d(TAG, "UDP receive connection closed or timed out: ${e.message}")
                    }
                }
            }
        }
    }
    
    /**
     * Send UNLOCK message back to PC Host to release mouse trap bounds.
     */
    fun sendUnlockCommand() {
        sendRawToHost("UNLOCK\n")
    }

    /**
     * Send INIT:width:height:densityDpi to the PC Host so it can calibrate its virtual
     * coordinate clamps and DPI scaling factor to the real Android screen.
     */
    fun sendInitPacket(width: Int, height: Int, densityDpi: Int) {
        sendRawToHost("INIT:$width:$height:$densityDpi\n")
        Log.d(TAG, "INIT packet sent: INIT:$width:$height:$densityDpi")
    }

    /** Thread-safe raw write to the TCP output stream. */
    private fun sendRawToHost(payload: String) {
        clientScope.launch(Dispatchers.IO) {
            try {
                val output = tcpOutputStream
                if (output != null) {
                    synchronized(output) {
                        output.write(payload.toByteArray(Charsets.UTF_8))
                        output.flush()
                    }
                } else {
                    Log.w(TAG, "Cannot send '$payload': TCP output stream unavailable.")
                }
            } catch (e: Exception) {
                Log.e(TAG, "Failed to send to host: ${e.message}")
            }
        }
    }

    
    /**
     * Parse raw string lines based on DeskStream KVM specification:
     * - Mouse Moves: `M:dx:dy`
     * - Mouse Clicks: `C:BUTTON:STATE` (STATE: 1=down, 0=up)
     * - Keyboard Text: `K:TEXT:[char]`
     * - Keyboard Action: `K:ACT:[KEY]`
     */
    private fun parseAndDispatch(message: String) {
        try {
            val parts = message.split(":")
            if (parts.isEmpty()) return
            
            when (parts[0]) {
                "M" -> {
                    if (parts.size >= 3) {
                        val dx = parts[1].toIntOrNull() ?: 0
                        val dy = parts[2].toIntOrNull() ?: 0
                        onMouseMoveReceived?.invoke(dx, dy)
                    }
                }
                "C" -> {
                    if (parts.size >= 3) {
                        val button = parts[1]
                        val state = parts[2].toIntOrNull() ?: 0
                        onMouseClickReceived?.invoke(button, state)
                    }
                }
                "K" -> {
                    if (parts.size >= 3) {
                        val subType = parts[1]
                        // Reconstruct standard string payload (might contain colons)
                        val content = parts.drop(2).joinToString(":")
                        when (subType) {
                            "TEXT" -> onKeyTextReceived?.invoke(content)
                            "ACT" -> onKeyActionReceived?.invoke(content)
                            else -> Log.w(TAG, "Unknown keyboard subtype: $subType")
                        }
                    }
                }
                // Scroll: S:dy  (dy > 0 = scroll down)
                "S" -> {
                    if (parts.size >= 2) {
                        val dy = parts[1].toIntOrNull() ?: 0
                        onMouseScrollReceived?.invoke(dy)
                    }
                }
                else -> {
                    Log.d(TAG, "Unknown payload type received: $message")
                }
            }
        } catch (e: Exception) {
            Log.e(TAG, "Failed to parse incoming network message '$message': ${e.message}")
        }
    }
    
    /**
     * Safely closes open TCP and UDP connections.
     */
    private fun cleanupSockets() {
        try {
            tcpOutputStream?.close()
        } catch (e: Exception) {}
        tcpOutputStream = null
        
        try {
            tcpSocket?.close()
        } catch (e: Exception) {}
        tcpSocket = null
        
        try {
            udpSocket?.close()
        } catch (e: Exception) {}
        udpSocket = null
    }
    
    companion object {
        private const val TAG = "SocketClient"
    }
}
