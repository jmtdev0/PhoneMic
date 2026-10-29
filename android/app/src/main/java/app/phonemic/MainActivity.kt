package app.phonemic

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.view.View
import android.view.ViewGroup.LayoutParams.MATCH_PARENT
import android.view.ViewGroup.LayoutParams.WRAP_CONTENT
import android.widget.Button
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.ScrollView
import android.widget.TextView
import java.net.ConnectException
import java.net.SocketTimeoutException
import javax.net.ssl.SSLException

/**
 * Pairing happens once: scan the QR code on the PC with the camera app, which opens this screen with a pairing
 * link. After that the phone only needs Start.
 */
class MainActivity : Activity() {
    private lateinit var credentials: Credentials
    private lateinit var unpairedGroup: LinearLayout
    private lateinit var pairedGroup: LinearLayout
    private lateinit var pcView: TextView
    private lateinit var toggle: Button
    private lateinit var statusView: TextView
    private lateinit var statsView: TextView
    private lateinit var certView: TextView
    private lateinit var meter: ProgressBar
    private lateinit var controlGroup: LinearLayout
    private lateinit var dictateButton: Button
    private lateinit var sendButton: Button
    private lateinit var controlHint: TextView
    private lateinit var controlStatusView: TextView
    private val ui = Handler(Looper.getMainLooper())
    private val refresh = object : Runnable {
        override fun run() {
            render()
            ui.postDelayed(this, 100)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        credentials = Credentials(getSharedPreferences(Credentials.PREFS, Context.MODE_PRIVATE))
        setContentView(buildLayout())
        handleIntent(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        handleIntent(intent)
    }

    override fun onResume() {
        super.onResume()
        ui.post(refresh)
    }

    override fun onPause() {
        ui.removeCallbacks(refresh)
        super.onPause()
    }

    private fun buildLayout(): ScrollView {
        val column = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(24), dp(48), dp(24), dp(24))
        }
        fun label(size: Float, bottom: Int = 8) = TextView(this).apply {
            textSize = size
            setPadding(0, 0, 0, dp(bottom))
        }
        column.addView(label(26f, 4).apply { text = "PhoneMic" })
        column.addView(label(14f, 12).apply { text = "Use this phone as the microphone of your PC." })
        statusView = label(16f, 16)
        column.addView(statusView)

        unpairedGroup = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        unpairedGroup.addView(label(16f, 12).apply { text = "This phone is not paired yet." })
        unpairedGroup.addView(label(14f, 12).apply {
            text = "1. On the PC, run PhoneMic and open http://127.0.0.1:8444 in the browser.\n" +
                "2. Scan the QR code on that page with this phone's camera app.\n" +
                "3. Tap the link it offers (it opens PhoneMic) and confirm the pairing."
        })
        column.addView(unpairedGroup)

        pairedGroup = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        pcView = label(16f, 12)
        pairedGroup.addView(pcView)
        toggle = Button(this).apply { setOnClickListener { onToggle() } }
        pairedGroup.addView(toggle, LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT))

        // Remote control of Claude Desktop on the PC: only while streaming, and only what the PC allows this phone.
        controlGroup = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        dictateButton = Button(this).apply { setOnClickListener { onDictate() } }
        sendButton = Button(this).apply {
            text = "Send"
            setOnClickListener { MicService.control(MicService.SEND) }
        }
        controlGroup.addView(dictateButton, LinearLayout.LayoutParams(0, WRAP_CONTENT, 1f))
        controlGroup.addView(sendButton, LinearLayout.LayoutParams(0, WRAP_CONTENT, 1f))
        pairedGroup.addView(controlGroup, LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT))
        controlHint = label(12f, 4).apply {
            setPadding(0, dp(8), 0, 0)
            text = "Dictate and Send are off for this phone. Turn them on for it in the PC's pairing page " +
                "to press Claude Desktop's microphone and send buttons from here."
        }
        pairedGroup.addView(controlHint)
        controlStatusView = label(14f, 4).apply { setPadding(0, dp(8), 0, 0) }
        pairedGroup.addView(controlStatusView)
        meter = ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal).apply { max = 100 }
        pairedGroup.addView(meter, LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT).apply { topMargin = dp(16) })
        statsView = label(12f, 8).apply { setPadding(0, dp(6), 0, dp(8)) }
        pairedGroup.addView(statsView)
        certView = label(11f, 8)
        pairedGroup.addView(certView)
        pairedGroup.addView(Button(this).apply {
            text = "Unpair this phone"
            setOnClickListener { confirmUnpair() }
        })
        pairedGroup.addView(label(12f, 0).apply {
            setPadding(0, dp(16), 0, 0)
            text = "Tip: for long sessions set this app's battery usage to Unrestricted in Android settings."
        })
        column.addView(pairedGroup)
        return ScrollView(this).apply { addView(column) }
    }

    private fun render() {
        val paired = credentials.isPaired
        unpairedGroup.visibility = if (paired) View.GONE else View.VISIBLE
        pairedGroup.visibility = if (paired) View.VISIBLE else View.GONE
        statusView.text = MicState.status
        if (!paired) return
        toggle.text = if (MicState.running) "Stop" else "Start"
        pcView.text = "Paired with ${credentials.pcName}\nLast address: ${credentials.host}"
        statsView.text = MicState.stats
        val live = MicState.running && MicState.ready
        val anyControl = MicState.canDictate || MicState.canSend
        controlGroup.visibility = if (live && anyControl) View.VISIBLE else View.GONE
        controlHint.visibility = if (live && !anyControl) View.VISIBLE else View.GONE
        controlStatusView.visibility = if (live && anyControl) View.VISIBLE else View.GONE
        dictateButton.text = if (MicState.dictating) "Stop dictating" else "Dictate"
        dictateButton.isEnabled = MicState.canDictate
        sendButton.isEnabled = MicState.canSend
        controlStatusView.text = MicState.controlStatus
        meter.progress = (MicState.level * 100).toInt().coerceIn(0, 100)
        MicState.level *= 0.85f
        certView.text = "PC key (compare with the PC window):\n${formatPin(credentials.pinHex.orEmpty())}"
    }

    // --- pairing -------------------------------------------------------------------------------------------

    private fun handleIntent(intent: Intent?) {
        val data = intent?.data ?: return
        intent.data = null // consumed: a rotation must not ask again
        val link = PairingLink.parse(data.toString())
        if (link == null) {
            MicState.status = "That pairing link is not valid."
            return
        }
        AlertDialog.Builder(this)
            .setTitle("Pair with ${link.pcName}?")
            .setMessage(
                "Address: ${link.host}\nPC key: ${formatPin(link.pinHex).take(KEY_PREVIEW_CHARS)}…\n\n" +
                    "Only continue if this is the PC you are looking at.",
            )
            .setPositiveButton("Pair") { _, _ -> pair(link) }
            .setNegativeButton("Cancel", null)
            .show()
    }

    private fun pair(link: PairingLink) {
        if (MicState.running) stopService(Intent(this, MicService::class.java))
        MicState.status = "Pairing…"
        Thread({
            try {
                val result = Enroller.enroll(link, deviceName(), DeviceKey.publicKeyDer())
                credentials.save(result.deviceId, link.pinHex, link.host, result.pcName)
                ui.post { MicState.status = "Paired with ${result.pcName}. Press Start." }
            } catch (error: Exception) {
                ui.post { MicState.status = "Pairing failed: ${explain(error, link)}" }
            }
        }, "phonemic-pair").start()
    }

    private fun explain(error: Exception, link: PairingLink): String = when {
        error is Enroller.PairingRefused -> error.message.orEmpty()
        generateSequence<Throwable>(error) { it.cause }.any { it.message == SpkiPinTrustManager.MISMATCH_MESSAGE } ->
            "The PC's key does not match the QR code. Show a new QR code on the PC."
        error is ConnectException || error is SocketTimeoutException ->
            "Could not reach ${link.host}. Are the phone and the PC on the same Wi-Fi, and is PhoneMic running?"
        error is SSLException -> "Secure connection failed: ${error.message}"
        else -> error.message ?: error.javaClass.simpleName
    }

    private fun deviceName(): String =
        Settings.Global.getString(contentResolver, "device_name")?.takeIf { it.isNotBlank() } ?: Build.MODEL

    private fun confirmUnpair() {
        AlertDialog.Builder(this)
            .setTitle("Unpair this phone?")
            .setMessage("You will have to scan a new QR code. You can also revoke this phone on the PC's pairing page.")
            .setPositiveButton("Unpair") { _, _ ->
                if (MicState.running) stopService(Intent(this, MicService::class.java))
                credentials.clear()
                DeviceKey.delete()
                MicState.fingerprint = null
                MicState.stats = ""
                MicState.status = "Unpaired."
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    // --- streaming -----------------------------------------------------------------------------------------

    private fun onToggle() {
        if (MicState.running) {
            stopService(Intent(this, MicService::class.java))
            return
        }
        val needed = buildList {
            add(Manifest.permission.RECORD_AUDIO)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) add(Manifest.permission.POST_NOTIFICATIONS)
        }.filter { checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }
        if (needed.isEmpty()) startStreaming() else requestPermissions(needed.toTypedArray(), REQUEST_PERMISSIONS)
    }

    @Deprecated("Deprecated in Java")
    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode != REQUEST_PERMISSIONS) return
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
            startStreaming()
        } else {
            MicState.status = "The microphone permission is required."
        }
    }

    private fun onDictate() {
        MicService.control(if (MicState.dictating) MicService.DICTATION_STOP else MicService.DICTATION_START)
    }

    private fun startStreaming() {
        MicState.status = "Connecting…"
        startForegroundService(Intent(this, MicService::class.java))
    }

    private fun dp(value: Int) = (value * resources.displayMetrics.density).toInt()

    companion object {
        private const val REQUEST_PERMISSIONS = 1
        private const val KEY_PREVIEW_CHARS = 23 // the first eight bytes of the pin
    }
}
