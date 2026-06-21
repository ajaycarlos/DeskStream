package com.deskstream.sync.service

import android.app.*
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.util.Log
import androidx.core.app.NotificationCompat
import com.deskstream.sync.event.InputEvent
import com.deskstream.sync.event.InputEventBus
import com.deskstream.sync.network.SocketClient
import kotlinx.coroutines.*

/**
 * Foreground Service that maintains the active network connection to the PC Host.
 * Ensures the connection stays active when the application is placed in the background
 * or the screen is turned off. Dispatches events to the application-wide event bus.
 */
class InputBridgeService : Service() {

    private var socketClient: SocketClient? = null
    private val serviceScope = CoroutineScope(Dispatchers.Default + SupervisorJob())

    override fun onCreate() {
        super.onCreate()
        Log.i(TAG, "InputBridgeService created")
        createNotificationChannel()
        isServiceRunning = true
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        Log.i(TAG, "InputBridgeService start command received")

        // Retrieve pairing parameters from launching Intent
        val modeStr = intent?.getStringExtra(EXTRA_CONNECTION_MODE) ?: "USB"
        val hostIp = intent?.getStringExtra(EXTRA_HOST_IP) ?: "127.0.0.1"
        val port = intent?.getIntExtra(EXTRA_PORT, 8080) ?: 8080

        val mode = try {
            SocketClient.ConnectionMode.valueOf(modeStr.uppercase())
        } catch (e: Exception) {
            SocketClient.ConnectionMode.USB
        }

        // Establish background protection by transitioning into a Foreground Service
        startServiceForeground()

        // Shutdown active socket client if it was already running
        stopSocketClient()

        // Spin up the Socket Client listener
        startSocketClient(mode, hostIp, port)

        return START_REDELIVER_INTENT
    }

    private fun startServiceForeground() {
        val notificationId = 1001
        
        // Define flags for the PendingIntent based on Android version
        val pendingIntentFlags = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        } else {
            PendingIntent.FLAG_UPDATE_CURRENT
        }
        
        // Dynamic application launch callback on notification tap
        val launchIntent = packageManager.getLaunchIntentForPackage(packageName)
        val pendingIntent = if (launchIntent != null) {
            PendingIntent.getActivity(this, 0, launchIntent, pendingIntentFlags)
        } else null

        val notification = NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("DeskStream Sync Active")
            .setContentText("KVM input listener is running in the background.")
            .setSmallIcon(android.R.drawable.ic_dialog_info) // System default icon
            .setOngoing(true)
            .setContentIntent(pendingIntent)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setCategory(NotificationCompat.CATEGORY_SERVICE)
            .build()

        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                // Declare FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE to match Android 14+ KVM architecture
                startForeground(
                    notificationId, 
                    notification, 
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE
                )
            } else {
                startForeground(notificationId, notification)
            }
            Log.d(TAG, "InputBridgeService successfully configured in the foreground.")
        } catch (e: Exception) {
            Log.e(TAG, "Critical failure transitioning service to foreground: ${e.message}", e)
        }
    }

    private fun startSocketClient(mode: SocketClient.ConnectionMode, hostIp: String, port: Int) {
        val client = SocketClient(mode, hostIp, port)
        socketClient = client

        // Wire network callbacks straight to the reactive InputEventBus
        client.onMouseMoveReceived = { dx, dy ->
            serviceScope.launch {
                InputEventBus.emit(InputEvent.MouseMove(dx, dy))
            }
        }

        client.onMouseClickReceived = { button, state ->
            serviceScope.launch {
                InputEventBus.emit(InputEvent.MouseClick(button, state))
            }
        }

        client.onMouseScrollReceived = { dy ->
            serviceScope.launch {
                InputEventBus.emit(InputEvent.MouseScroll(dy))
            }
        }

        client.onKeyTextReceived = { text ->
            serviceScope.launch {
                InputEventBus.emit(InputEvent.KeyText(text))
            }
        }

        client.onKeyActionReceived = { action ->
            serviceScope.launch {
                InputEventBus.emit(InputEvent.KeyAction(action))
            }
        }

        client.onConnectionStateChanged = { state, error ->
            serviceScope.launch {
                InputEventBus.emit(InputEvent.ConnectionStateChanged(state, error))
            }
        }

        // Listen for exit/unlock event triggers from AccessibilityService to release PC mouse trap
        serviceScope.launch {
            InputEventBus.unlockEvents.collect {
                client.sendUnlockCommand()
            }
        }

        // ── Bug 2 Fix: forward INIT:w:h packet when AccessibilityService reports screen size ──
        serviceScope.launch {
            InputEventBus.initEvents.collect { (w, h) ->
                client.sendInitPacket(w, h)
            }
        }

        client.start()
    }

    private fun stopSocketClient() {
        socketClient?.stop()
        socketClient = null
    }

    override fun onDestroy() {
        Log.i(TAG, "InputBridgeService destroyed")
        stopSocketClient()
        serviceScope.cancel()
        isServiceRunning = false
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? {
        return null // We rely on startService lifecycle
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channelName = "DeskStream Sync Background Service"
            val descriptionText = "Monitors socket connection for PC-to-Android KVM streams"
            val importance = NotificationManager.IMPORTANCE_LOW
            val channel = NotificationChannel(CHANNEL_ID, channelName, importance).apply {
                description = descriptionText
                setShowBadge(false)
            }
            val notificationManager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            notificationManager.createNotificationChannel(channel)
        }
    }

    companion object {
        private const val TAG = "InputBridgeService"
        private const val CHANNEL_ID = "deskstream_sync_channel"

        @Volatile
        var isServiceRunning = false

        const val EXTRA_CONNECTION_MODE = "extra_connection_mode"
        const val EXTRA_HOST_IP = "extra_host_ip"
        const val EXTRA_PORT = "extra_port"

        /**
         * Safe utility helper to initialize and launch the Foreground Input Bridge Service.
         */
        fun startService(context: Context, mode: String, hostIp: String, port: Int) {
            val intent = Intent(context, InputBridgeService::class.java).apply {
                putExtra(EXTRA_CONNECTION_MODE, mode)
                putExtra(EXTRA_HOST_IP, hostIp)
                putExtra(EXTRA_PORT, port)
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
        }

        /**
         * Safe utility helper to stop the Foreground Input Bridge Service.
         */
        fun stopService(context: Context) {
            val intent = Intent(context, InputBridgeService::class.java)
            context.stopService(intent)
        }
    }
}
