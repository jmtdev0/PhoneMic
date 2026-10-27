package app.phonemic

import android.Manifest
import android.app.Activity
import android.content.Context
import android.content.Intent
import android.content.SharedPreferences
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.text.InputFilter
import android.text.InputType
import android.view.Gravity
import android.view.ViewGroup.LayoutParams.MATCH_PARENT
import android.view.ViewGroup.LayoutParams.WRAP_CONTENT
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.ScrollView
import android.widget.TextView

/** One screen: PC address, pairing code and a Start/Stop button. */
class MainActivity : Activity() {
    private lateinit var prefs: SharedPreferences
    private lateinit var hostInput: EditText
    private lateinit var codeInput: EditText
    private lateinit var toggle: Button
    private lateinit var statusView: TextView
    private lateinit var certView: TextView
    private lateinit var statsView: TextView
    private lateinit var meter: ProgressBar
    private val ui = Handler(Looper.getMainLooper())
    private val refresh = object : Runnable {
        override fun run() {
            render()
            ui.postDelayed(this, 100)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        setContentView(buildLayout())
        hostInput.setText(prefs.getString(KEY_HOST, ""))
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
        fun text(size: Float, bottom: Int = 8) = TextView(this).apply {
            textSize = size
            setPadding(0, 0, 0, dp(bottom))
        }

        column.addView(text(26f, 4).apply { this.text = "PhoneMic" })
        column.addView(text(14f, 20).apply { this.text = "Use this phone as the microphone of your PC." })

        column.addView(text(13f, 2).apply { this.text = "PC address (shown in the PC window)" })
        hostInput = EditText(this).apply {
            hint = "192.168.1.38:8443"
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI
            setSingleLine()
        }
        column.addView(hostInput, LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT))

        column.addView(text(13f, 2).apply { this.text = "Pairing code"; setPadding(0, dp(12), 0, dp(2)) })
        codeInput = EditText(this).apply {
            hint = "000000"
            inputType = InputType.TYPE_CLASS_NUMBER
            filters = arrayOf(InputFilter.LengthFilter(CODE_LENGTH))
            gravity = Gravity.CENTER
            textSize = 24f
        }
        column.addView(codeInput, LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT))

        toggle = Button(this).apply { setOnClickListener { onToggle() } }
        column.addView(toggle, LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT).apply { topMargin = dp(16) })

        statusView = text(16f, 4).apply { setPadding(0, dp(16), 0, dp(4)) }
        column.addView(statusView)
        meter = ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal).apply { max = 100 }
        column.addView(meter, LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT))
        statsView = text(12f, 12).apply { setPadding(0, dp(6), 0, dp(12)) }
        column.addView(statsView)

        certView = text(11f, 8)
        column.addView(certView)
        column.addView(Button(this).apply {
            text = "Forget certificate"
            setOnClickListener { forgetCertificate() }
        })
        column.addView(text(12f, 0).apply {
            setPadding(0, dp(16), 0, 0)
            this.text = "Tip: for long sessions set this app's battery usage to Unrestricted in Android settings."
        })

        return ScrollView(this).apply { addView(column) }
    }

    private fun render() {
        val running = MicState.running
        toggle.text = if (running) "Stop" else "Start"
        hostInput.isEnabled = !running
        codeInput.isEnabled = !running
        statusView.text = MicState.status
        statsView.text = MicState.stats
        meter.progress = (MicState.level * 100).toInt().coerceIn(0, 100)
        MicState.level *= 0.85f
        certView.text = MicState.fingerprint?.let { "Server certificate SHA-256 (compare with the PC window):\n$it" }
            ?: "Server certificate: not seen yet"
    }

    private fun onToggle() {
        if (MicState.running) {
            stopService(Intent(this, MicService::class.java))
            return
        }
        val host = normalizeHost(hostInput.text.toString())
        val code = codeInput.text.toString().trim()
        if (host.isEmpty()) {
            MicState.status = "Enter the PC address shown in the PC window."
            return
        }
        if (code.length != CODE_LENGTH) {
            MicState.status = "Enter the 6-digit pairing code."
            return
        }
        prefs.edit().putString(KEY_HOST, host).apply()
        val needed = buildList {
            add(Manifest.permission.RECORD_AUDIO)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) add(Manifest.permission.POST_NOTIFICATIONS)
        }.filter { checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }
        if (needed.isEmpty()) startStreaming(host, code) else requestPermissions(needed.toTypedArray(), REQUEST_PERMISSIONS)
    }

    @Deprecated("Deprecated in Java")
    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode != REQUEST_PERMISSIONS) return
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
            startStreaming(normalizeHost(hostInput.text.toString()), codeInput.text.toString().trim())
        } else {
            MicState.status = "The microphone permission is required."
        }
    }

    private fun startStreaming(host: String, code: String) {
        MicState.status = "Connecting…"
        startForegroundService(
            Intent(this, MicService::class.java)
                .putExtra(MicService.EXTRA_HOST, host)
                .putExtra(MicService.EXTRA_CODE, code),
        )
    }

    private fun forgetCertificate() {
        val host = normalizeHost(hostInput.text.toString())
        prefs.edit().remove(PinningTrustManager.pinKey(host)).apply()
        MicState.fingerprint = null
        MicState.status = "Certificate forgotten. It will be trusted again on the next connection."
    }

    /** Accepts "192.168.1.38:8443", "https://192.168.1.38:8443/" and similar pasted forms. */
    private fun normalizeHost(raw: String): String =
        raw.trim().removePrefix("https://").removePrefix("http://").trimEnd('/')

    private fun dp(value: Int) = (value * resources.displayMetrics.density).toInt()

    companion object {
        const val PREFS = "phonemic"
        private const val KEY_HOST = "host"
        private const val CODE_LENGTH = 6
        private const val REQUEST_PERMISSIONS = 1
    }
}
