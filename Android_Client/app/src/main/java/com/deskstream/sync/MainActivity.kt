package com.deskstream.sync

import android.accessibilityservice.AccessibilityService
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.provider.Settings
import android.text.TextUtils
import android.util.Log
import android.view.inputmethod.InputMethodManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalLifecycleOwner
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import com.deskstream.sync.event.InputEvent
import com.deskstream.sync.event.InputEventBus
import com.deskstream.sync.network.SocketClient
import com.deskstream.sync.service.InputBridgeService
import com.deskstream.sync.service.KeyboardInjectionService
import com.deskstream.sync.service.MouseAccessibilityService
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            MaterialTheme(
                colorScheme = darkColorScheme(
                    primary = Color(0xFF6366F1), // Sleek indigo accent
                    onPrimary = Color.White,
                    surface = Color(0xFF1E1B4B), // Premium deep dark surface
                    background = Color(0xFF0F0C1B) // Deep space background
                )
            ) {
                Surface(
                    modifier = Modifier.fillMaxSize(),
                    color = MaterialTheme.colorScheme.background
                ) {
                    KvmDashboard()
                }
            }
        }
    }
}

@Composable
fun KvmDashboard() {
    val context = LocalContext.current
    val lifecycleOwner = LocalLifecycleOwner.current
    val coroutineScope = rememberCoroutineScope()

    // 1. Permission status states
    var isAccessibilityEnabled by remember { mutableStateOf(false) }
    var isImeEnabled by remember { mutableStateOf(false) }
    var isImeSelected by remember { mutableStateOf(false) }

    // 2. Connection parameter states
    var connectionMode by remember { mutableStateOf("USB") } // "USB" or "WIFI"
    var hostIp by remember { mutableStateOf("192.168.1.100") }
    var portStr by remember { mutableStateOf("8080") }

    // 3. Service & Socket state observers
    var isServiceRunning by remember { mutableStateOf(false) }
    var socketState by remember { mutableStateOf(SocketClient.ConnectionState.DISCONNECTED) }
    var lastError by remember { mutableStateOf<String?>(null) }

    // Function to run permission checks
    val updatePermissionStatuses = {
        isAccessibilityEnabled = isAccessibilityServiceEnabled(context, MouseAccessibilityService::class.java)
        isImeEnabled = isImeEnabled(context, context.packageName, KeyboardInjectionService::class.java.name)
        isImeSelected = isImeSelected(context, context.packageName, KeyboardInjectionService::class.java.name)
        isServiceRunning = InputBridgeService.isServiceRunning
        if (!isServiceRunning) {
            socketState = SocketClient.ConnectionState.DISCONNECTED
        }
    }

    // Lifecycle observer to re-check permissions and service state when returning to the app
    DisposableEffect(lifecycleOwner) {
        val observer = LifecycleEventObserver { _, event ->
            if (event == Lifecycle.Event.ON_RESUME || event == Lifecycle.Event.ON_START) {
                updatePermissionStatuses()
            }
        }
        lifecycleOwner.lifecycle.addObserver(observer)
        onDispose {
            lifecycleOwner.lifecycle.removeObserver(observer)
        }
    }

    // Collect connection status updates from the central InputEventBus
    LaunchedEffect(Unit) {
        InputEventBus.events.collect { event ->
            if (event is InputEvent.ConnectionStateChanged) {
                socketState = event.state
                lastError = event.error
            }
        }
    }

    // Security check: Local Address Isolation validation
    val isIpValid = remember(hostIp, connectionMode) {
        if (connectionMode == "USB") true
        else {
            // Check if entered value is a valid private IPv4 subnet or localhost format
            val parts = hostIp.split(".")
            if (parts.size == 4) {
                val p1 = parts[0].toIntOrNull()
                val p2 = parts[1].toIntOrNull()
                val p3 = parts[2].toIntOrNull()
                val p4 = parts[3].toIntOrNull()
                
                if (p1 != null && p2 != null && p3 != null && p4 != null &&
                    p1 in 0..255 && p2 in 0..255 && p3 in 0..255 && p4 in 0..255) {
                    
                    // CYBERSECURITY: Check local address boundaries (RFC 1918 + loopback)
                    // 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, or 127.0.0.1
                    val isPrivate = p1 == 10 || 
                                    (p1 == 172 && p2 in 16..31) || 
                                    (p1 == 192 && p2 == 168) || 
                                    (p1 == 127)
                    isPrivate
                } else false
            } else false
        }
    }

    val isPortValid = remember(portStr) {
        val p = portStr.toIntOrNull()
        p != null && p in 1024..65535
    }

    val canStartService = isAccessibilityEnabled && isImeEnabled && isImeSelected && isIpValid && isPortValid

    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.spacedBy(16.dp)
    ) {
        // App Header Banner
        Box(
            modifier = Modifier
                .fillMaxWidth()
                .background(
                    brush = Brush.horizontalGradient(
                        colors = listOf(Color(0xFF6366F1), Color(0xFF4F46E5))
                    ),
                    shape = RoundedCornerShape(16.dp)
                )
                .padding(24.dp)
        ) {
            Column {
                Text(
                    text = "DeskStream Sync",
                    color = Color.White,
                    fontSize = 28.sp,
                    fontWeight = FontWeight.Bold
                )
                Text(
                    text = "Seamless PC-to-Android Input Bridge",
                    color = Color.White.copy(alpha = 0.8f),
                    fontSize = 14.sp
                )
            }
        }

        // Connection Status Card
        Card(
            modifier = Modifier.fillMaxWidth(),
            shape = RoundedCornerShape(16.dp),
            colors = CardDefaults.cardColors(
                containerColor = when {
                    !isServiceRunning -> Color(0xFF2E2E3E)
                    socketState == SocketClient.ConnectionState.CONNECTED -> Color(0xFF065F46) // Dark green
                    socketState == SocketClient.ConnectionState.CONNECTING -> Color(0xFF78350F) // Dark amber
                    else -> Color(0xFF991B1B) // Dark red
                }
            )
        ) {
            Column(modifier = Modifier.padding(16.dp)) {
                Text(
                    text = "Status: " + when {
                        !isServiceRunning -> "BRIDGE STOPPED"
                        socketState == SocketClient.ConnectionState.CONNECTED -> "CONNECTED"
                        socketState == SocketClient.ConnectionState.CONNECTING -> "CONNECTING..."
                        else -> "DISCONNECTED"
                    },
                    color = Color.White,
                    fontSize = 18.sp,
                    fontWeight = FontWeight.Bold
                )
                
                Spacer(modifier = Modifier.height(4.dp))
                
                val displayIp = if (connectionMode == "USB") "127.0.0.1" else hostIp
                Text(
                    text = "Connection Route: $connectionMode ($displayIp:$portStr)",
                    color = Color.White.copy(alpha = 0.9f),
                    fontSize = 14.sp
                )

                lastError?.let {
                    if (isServiceRunning && socketState != SocketClient.ConnectionState.CONNECTED) {
                        Spacer(modifier = Modifier.height(8.dp))
                        Text(
                            text = "Error: $it",
                            color = Color(0xFFFCA5A5), // Light red text
                            fontSize = 13.sp,
                            fontWeight = FontWeight.SemiBold
                        )
                    }
                }
            }
        }

        // Transport Mode & Configurations
        Card(
            modifier = Modifier.fillMaxWidth(),
            shape = RoundedCornerShape(16.dp),
            colors = CardDefaults.cardColors(containerColor = Color(0xFF1E1B4B))
        ) {
            Column(
                modifier = Modifier.padding(16.dp),
                verticalArrangement = Arrangement.spacedBy(12.dp)
            ) {
                Text(
                    text = "Network Configurations",
                    fontSize = 16.sp,
                    fontWeight = FontWeight.Bold,
                    color = Color.White
                )

                // Mode Selector Toggle
                Row(
                    modifier = Modifier
                        .fillMaxWidth()
                        .background(Color(0xFF0F0C1B), RoundedCornerShape(8.dp))
                        .padding(4.dp)
                ) {
                    listOf("USB", "WIFI").forEach { mode ->
                        Button(
                            onClick = { 
                                connectionMode = mode 
                                lastError = null
                            },
                            modifier = Modifier
                                .weight(1f)
                                .height(38.dp),
                            shape = RoundedCornerShape(6.dp),
                            colors = ButtonDefaults.buttonColors(
                                containerColor = if (connectionMode == mode) Color(0xFF6366F1) else Color.Transparent,
                                contentColor = if (connectionMode == mode) Color.White else Color.Gray
                            ),
                            contentPadding = PaddingValues(0.dp)
                        ) {
                            Text(text = if (mode == "USB") "USB (ADB reverse)" else "Wi-Fi Socket")
                        }
                    }
                }

                if (connectionMode == "WIFI") {
                    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                        OutlinedTextField(
                            value = hostIp,
                            onValueChange = { 
                                hostIp = it.trim() 
                                lastError = null
                            },
                            label = { Text("PC Host IP Address") },
                            modifier = Modifier.fillMaxWidth(),
                            isError = !isIpValid,
                            singleLine = true,
                            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Phone)
                        )
                        
                        // CyberSecurity Warnings for IP entries
                        if (!isIpValid) {
                            Text(
                                text = "Security Warning: IP address must be a valid private network block (e.g. 192.168.x.x or 10.x.x.x) to block unauthorized data routing.",
                                color = Color(0xFFF87171),
                                fontSize = 11.sp,
                                modifier = Modifier.padding(horizontal = 4.dp)
                            )
                        }
                    }
                } else {
                    Text(
                        text = "Note: USB Mode forwards connections securely over ADB. Run 'adb reverse tcp:8080 tcp:8080' on the PC to initialize client tunneling.",
                        color = Color.White.copy(alpha = 0.7f),
                        fontSize = 12.sp,
                        modifier = Modifier.padding(vertical = 4.dp)
                    )
                }

                OutlinedTextField(
                    value = portStr,
                    onValueChange = { 
                        portStr = it.trim() 
                        lastError = null
                    },
                    label = { Text("Port") },
                    modifier = Modifier.fillMaxWidth(),
                    isError = !isPortValid,
                    singleLine = true,
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number)
                )
            }
        }

        // Setup Checklist Card
        Card(
            modifier = Modifier.fillMaxWidth(),
            shape = RoundedCornerShape(16.dp),
            colors = CardDefaults.cardColors(containerColor = Color(0xFF1E1B4B))
        ) {
            Column(
                modifier = Modifier.padding(16.dp),
                verticalArrangement = Arrangement.spacedBy(16.dp)
            ) {
                Text(
                    text = "System Integration Checklist",
                    fontSize = 16.sp,
                    fontWeight = FontWeight.Bold,
                    color = Color.White
                )

                // 1. Accessibility Service Config Card Row
                PermissionItem(
                    title = "Virtual Mouse Overlay",
                    description = "Enables floating pointer draw layer and Accessibility click gestures.",
                    isGranted = isAccessibilityEnabled,
                    actionText = "Enable",
                    onAction = {
                        val intent = Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)
                        context.startActivity(intent)
                    }
                )

                Divider(color = Color.White.copy(alpha = 0.1f))

                // 2. Keyboard IME Service Card Row
                val imeStatusText = when {
                    !isImeEnabled -> "Disabled"
                    !isImeSelected -> "Not Default"
                    else -> "Active"
                }
                PermissionItem(
                    title = "Invisible Keyboard Injection",
                    description = "Enables custom Input Method Service to inject keystrokes. Status: $imeStatusText",
                    isGranted = isImeEnabled && isImeSelected,
                    actionText = if (!isImeEnabled) "Configure" else "Set Default",
                    onAction = {
                        if (!isImeEnabled) {
                            val intent = Intent(Settings.ACTION_INPUT_METHOD_SETTINGS)
                            context.startActivity(intent)
                        } else {
                            val imm = context.getSystemService(Context.INPUT_METHOD_SERVICE) as InputMethodManager
                            imm.showInputMethodPicker()
                            // ── Bug 3 Fix: bounded polling tied strictly to this picker interaction ──
                            // The system dialog is a transparent overlay so the Activity never pauses
                            // (no ON_PAUSE/ON_RESUME fires). We poll the raw Settings DB for up to
                            // 15 s and break the moment the user confirms their selection, keeping
                            // the check completely isolated to this user action window.
                            coroutineScope.launch {
                                repeat(30) { // 30 × 500ms = 15 seconds max
                                    delay(500)
                                    updatePermissionStatuses()
                                    if (isImeSelected) return@launch // early exit
                                }
                            }
                        }
                    }
                )
            }
        }

        Spacer(modifier = Modifier.weight(1f))

        // Main Activation Action Button
        Button(
            onClick = {
                if (isServiceRunning) {
                    InputBridgeService.stopService(context)
                    isServiceRunning = false
                    socketState = SocketClient.ConnectionState.DISCONNECTED
                    lastError = null

                    // Prompt user to switch back to their normal keyboard
                    val imm = context.getSystemService(Context.INPUT_METHOD_SERVICE) as InputMethodManager
                    imm.showInputMethodPicker()
                    // ── Bug 3 (Stop path): same bounded polling as "Set Default" ──
                    // The IME picker is a transparent overlay — the Activity never pauses,
                    // so ON_RESUME never fires. Poll for up to 15 s and exit early the
                    // moment the user selects a different keyboard (isImeSelected → false).
                    coroutineScope.launch {
                        repeat(30) { // 30 × 500ms = 15 seconds max
                            delay(500)
                            updatePermissionStatuses()
                            if (!isImeSelected) return@launch // early exit: our IME is no longer default
                        }
                    }
                } else {
                    lastError = null
                    InputBridgeService.startService(
                        context = context,
                        mode = connectionMode,
                        hostIp = hostIp,
                        port = portStr.toIntOrNull() ?: 8080
                    )
                    isServiceRunning = true
                }
            },
            modifier = Modifier
                .fillMaxWidth()
                .height(56.dp),
            shape = RoundedCornerShape(12.dp),
            enabled = isServiceRunning || canStartService,
            colors = ButtonDefaults.buttonColors(
                containerColor = if (isServiceRunning) Color(0xFFEF4444) else Color(0xFF6366F1),
                disabledContainerColor = Color(0xFF2E2E3E)
            )
        ) {
            Text(
                text = if (isServiceRunning) "Stop Input Bridge" else "Start Input Bridge",
                fontSize = 16.sp,
                fontWeight = FontWeight.Bold,
                color = if (isServiceRunning || canStartService) Color.White else Color.Gray
            )
        }

        if (!isServiceRunning && !canStartService) {
            Text(
                text = "Ensure all permissions are enabled and input parameters are valid to initialize KVM.",
                color = Color.Gray,
                fontSize = 12.sp,
                modifier = Modifier.padding(horizontal = 16.dp)
            )
        }
    }
}

@Composable
fun PermissionItem(
    title: String,
    description: String,
    isGranted: Boolean,
    actionText: String,
    onAction: () -> Unit
) {
    Row(
        modifier = Modifier.fillMaxWidth(),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(12.dp)
    ) {
        // Status indicator
        Text(
            text = if (isGranted) "✅" else "⚠️",
            fontSize = 24.sp
        )

        Column(modifier = Modifier.weight(1f)) {
            Text(
                text = title,
                fontSize = 14.sp,
                fontWeight = FontWeight.Bold,
                color = Color.White
            )
            Text(
                text = description,
                fontSize = 12.sp,
                color = Color.LightGray
            )
        }

        if (!isGranted) {
            Button(
                onClick = onAction,
                shape = RoundedCornerShape(8.dp),
                colors = ButtonDefaults.buttonColors(
                    containerColor = Color(0xFF4F46E5),
                    contentColor = Color.White
                ),
                contentPadding = PaddingValues(horizontal = 12.dp, vertical = 6.dp),
                modifier = Modifier.height(36.dp)
            ) {
                Text(text = actionText, fontSize = 12.sp, fontWeight = FontWeight.Bold)
            }
        }
    }
}

// ==========================================
// System Permission Verification Helper Methods
// ==========================================

fun isAccessibilityServiceEnabled(context: Context, serviceClass: Class<out AccessibilityService>): Boolean {
    val expectedComponentName = ComponentName(context, serviceClass)
    val enabledServicesSetting = Settings.Secure.getString(
        context.contentResolver,
        Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES
    ) ?: return false

    val colonSplitter = TextUtils.SimpleStringSplitter(':')
    colonSplitter.setString(enabledServicesSetting)
    while (colonSplitter.hasNext()) {
        val componentNameString = colonSplitter.next()
        val enabledService = ComponentName.unflattenFromString(componentNameString)
        if (enabledService != null && enabledService == expectedComponentName) {
            return true
        }
    }
    return false
}

fun isImeEnabled(context: Context, servicePackage: String, serviceClass: String): Boolean {
    val enabledImes = Settings.Secure.getString(
        context.contentResolver,
        Settings.Secure.ENABLED_INPUT_METHODS
    ) ?: return false

    val targetImeId = ComponentName(servicePackage, serviceClass).flattenToShortString()
    return enabledImes.contains(targetImeId)
}

fun isImeSelected(context: Context, servicePackage: String, serviceClass: String): Boolean {
    val currentImeId = Settings.Secure.getString(
        context.contentResolver,
        Settings.Secure.DEFAULT_INPUT_METHOD
    ) ?: return false
    
    val targetImeId = ComponentName(servicePackage, serviceClass).flattenToShortString()
    return currentImeId.startsWith(targetImeId) || currentImeId.contains(targetImeId)
}
