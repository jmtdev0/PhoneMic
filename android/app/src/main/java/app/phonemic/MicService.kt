package app.phonemic

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.graphics.drawable.Icon
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.net.wifi.WifiManager
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import android.util.Base64
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString.Companion.toByteString
import org.json.JSONObject
import java.util.concurrent.TimeUnit
import kotlin.math.abs
import kotlin.math.max
import kotlin.math.min

/**
 * Foreground service that captures the microphone and streams it to the paired PC.
 *
 * It authenticates with this phone's Keystore key (the PC sends a challenge, the phone signs it), then sends 20 ms
 * chunks of little-endian int16 mono PCM. If the PC is not where it used to be it looks for it on the Wi-Fi
 * network. Being a foreground service of type "microphone", it keeps capturing with the screen off.
 */
class MicService : Service() {
    private val main = Handler(Looper.getMainLooper())
    private lateinit var credentials: Credentials
    private var client: OkHttpClient? = null
    @Volatile private var socket: WebSocket? = null
    @Volatile private var ready = false
    @Volatile private var running = false
    private var captureThread: Thread? = null
    private var wakeLock: PowerManager.WakeLock? = null
    private var wifiLock: WifiManager.WifiLock? = null
    private var retryDelayMs = INITIAL_RETRY_MS
    private var failures = 0
    private var currentHost = ""
    private var deviceId = ""
    private var pinHex = ""

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            stopSelf()
            return START_NOT_STICKY
        }
        credentials = Credentials(getSharedPreferences(Credentials.PREFS, Context.MODE_PRIVATE))
        if (running) return START_NOT_STICKY
        if (!credentials.isPaired) {
            // The service must not stay in the foreground without a notification.
            startInForeground()
            fatal("This phone is not paired. Scan the QR code on the PC.")
            return START_NOT_STICKY
        }
        deviceId = credentials.deviceId!!
        pinHex = credentials.pinHex!!
        currentHost = credentials.host!!
        startInForeground()
        acquireLocks()
        client = PinnedTls.clientBuilder(SpkiPinTrustManager(pinHex) { MicState.fingerprint = it })
            .pingInterval(10, TimeUnit.SECONDS)
            .readTimeout(0, TimeUnit.MILLISECONDS)
            .build()
        running = true
        failures = 0
        MicState.running = true
        MicState.stats = ""
        startCapture()
        connect()
        // The phone can always reconnect by itself, but a restart by the system has no UI to ask for permissions.
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        running = false
        ready = false
        main.removeCallbacksAndMessages(null)
        socket?.cancel()
        socket = null
        captureThread?.join(500)
        wakeLock?.takeIf { it.isHeld }?.release()
        wifiLock?.takeIf { it.isHeld }?.release()
        MicState.running = false
        MicState.level = 0f
        if (MicState.status.startsWith("Streaming") || MicState.status.startsWith("Connecting")) {
            MicState.status = "Stopped"
        }
        super.onDestroy()
    }

    private fun connect() {
        if (!running) return
        ready = false
        MicState.status = "Connecting…"
        val request = Request.Builder().url("https://$currentHost/ws").build()
        socket = client!!.newWebSocket(request, listener)
    }

    private val listener = object : WebSocketListener() {
        override fun onOpen(webSocket: WebSocket, response: Response) {
            val hello = JSONObject().put("type", "hello").put("device", deviceId).put("sampleRate", SAMPLE_RATE)
            webSocket.send(hello.toString())
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            val message = JSONObject(text)
            when (message.optString("type")) {
                "challenge" -> {
                    val signature = DeviceKey.sign(message.getString("nonce"))
                    val auth = JSONObject()
                        .put("type", "auth")
                        .put("signature", Base64.encodeToString(signature, Base64.NO_WRAP))
                    webSocket.send(auth.toString())
                }
                "ready" -> {
                    retryDelayMs = INITIAL_RETRY_MS
                    failures = 0
                    ready = true
                    MicState.status = "Streaming to ${credentials.pcName}"
                }
                "error" -> MicState.status = "PC says: ${message.optString("reason")}"
                "stats" -> MicState.stats =
                    "Buffer ${message.optInt("bufferedMs")} ms · underruns ${message.optInt("underruns")} · " +
                        "dropped ${message.optInt("droppedMs")} ms"
            }
        }

        override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
            webSocket.close(1000, null)
        }

        override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
            ready = false
            when (code) {
                4403 -> {
                    credentials.clear()
                    DeviceKey.delete()
                    fatal("The PC no longer recognises this phone (it was revoked). Scan the QR code again to pair.")
                }
                else -> FATAL_CLOSE_REASONS[code]?.let { fatal(it) } ?: scheduleReconnect()
            }
        }

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            ready = false
            failures++
            // The first failure, and every third after it, may mean the PC changed address: look for it.
            if (failures == 1 || failures % 3 == 0) lookForPc() else scheduleReconnect()
        }
    }

    private fun lookForPc() {
        if (!running) return
        MicState.status = "Looking for your PC…"
        val port = currentHost.substringAfterLast(':').toIntOrNull() ?: DEFAULT_PORT
        Thread({
            val found = Discovery.find(applicationContext, port, pinHex) { !running }
            main.post {
                if (!running) return@post
                if (found != null) {
                    if (found != currentHost) {
                        currentHost = found
                        credentials.host = found
                    }
                    connect()
                } else {
                    MicState.status = "Could not find your PC on this Wi-Fi network. Retrying…"
                    scheduleReconnect()
                }
            }
        }, "phonemic-discovery").start()
    }

    private fun scheduleReconnect() {
        if (!running) return
        MicState.status = "Connection lost. Retrying…"
        main.postDelayed({ connect() }, retryDelayMs)
        retryDelayMs = min(retryDelayMs * 2, MAX_RETRY_MS)
    }

    private fun fatal(message: String) {
        MicState.status = message
        main.post { stopSelf() }
    }

    private fun startCapture() {
        val minBuffer = AudioRecord.getMinBufferSize(SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        val record = AudioRecord(
            MediaRecorder.AudioSource.VOICE_RECOGNITION,
            SAMPLE_RATE,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
            max(minBuffer, CHUNK_SAMPLES * 2 * 8),
        )
        if (record.state != AudioRecord.STATE_INITIALIZED) {
            record.release()
            fatal("Could not open the microphone.")
            return
        }
        captureThread = Thread({
            val samples = ShortArray(CHUNK_SAMPLES)
            val bytes = ByteArray(CHUNK_SAMPLES * 2)
            record.startRecording()
            while (running) {
                var filled = 0
                while (filled < samples.size && running) {
                    val read = record.read(samples, filled, samples.size - filled)
                    if (read < 0) {
                        fatal("The microphone stopped working.")
                        running = false
                        break
                    }
                    filled += read
                }
                if (filled < samples.size) break
                var peak = 0
                for (i in samples.indices) {
                    val value = samples[i].toInt()
                    peak = max(peak, abs(value))
                    bytes[2 * i] = (value and 0xFF).toByte()
                    bytes[2 * i + 1] = (value shr 8).toByte()
                }
                MicState.level = max(MicState.level * 0.9f, peak / 32768f)
                val current = socket
                // Drop audio instead of letting latency build up when the network cannot keep up.
                if (ready && current != null && current.queueSize() < MAX_QUEUED_BYTES) {
                    current.send(bytes.toByteString(0, bytes.size))
                }
            }
            record.stop()
            record.release()
        }, "phonemic-capture").also {
            it.priority = Thread.MAX_PRIORITY
            it.start()
        }
    }

    private fun startInForeground() {
        val manager = getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(
            NotificationChannel(CHANNEL_ID, "Microphone streaming", NotificationManager.IMPORTANCE_LOW),
        )
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE,
        )
        val stop = PendingIntent.getService(
            this, 1, Intent(this, MicService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val notification = Notification.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_mic)
            .setContentTitle("PhoneMic")
            .setContentText("Sending this phone's microphone to the PC")
            .setOngoing(true)
            .setContentIntent(open)
            .addAction(Notification.Action.Builder(Icon.createWithResource(this, R.drawable.ic_mic), "Stop", stop).build())
            .build()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE)
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }
    }

    @Suppress("DEPRECATION")
    private fun acquireLocks() {
        wakeLock = (getSystemService(Context.POWER_SERVICE) as PowerManager)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "phonemic:capture")
            .apply { acquire(MAX_SESSION_MS) }
        wifiLock = (applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager)
            .createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "phonemic:wifi")
            .apply { acquire() }
    }

    companion object {
        const val ACTION_STOP = "app.phonemic.STOP"

        private const val SAMPLE_RATE = 48000
        private const val CHUNK_SAMPLES = 960            // 20 ms
        private const val DEFAULT_PORT = 8443
        private const val MAX_QUEUED_BYTES = 128 * 1024L
        private const val INITIAL_RETRY_MS = 1000L
        private const val MAX_RETRY_MS = 5000L
        private const val MAX_SESSION_MS = 6 * 60 * 60 * 1000L
        private const val CHANNEL_ID = "mic"
        private const val NOTIFICATION_ID = 1

        private val FATAL_CLOSE_REASONS = mapOf(
            4400 to "The PC did not understand the connection.",
            4401 to "The PC rejected this phone's signature. Scan the QR code again to pair.",
            4409 to "Another phone took over the connection.",
            4500 to "The PC could not open its audio output.",
        )
    }
}
