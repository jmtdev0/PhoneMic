package app.phonemic

import android.content.SharedPreferences

/** What the phone remembers about the PC it is paired with. The private key lives in the Keystore, not here. */
class Credentials(private val prefs: SharedPreferences) {
    val deviceId: String? get() = prefs.getString(KEY_DEVICE_ID, null)
    val pinHex: String? get() = prefs.getString(KEY_PIN, null)
    val pcName: String get() = prefs.getString(KEY_PC_NAME, null) ?: "your PC"

    /** Last known address; updated when the PC is found again after its IP changed. */
    var host: String?
        get() = prefs.getString(KEY_HOST, null)
        set(value) = prefs.edit().putString(KEY_HOST, value).apply()

    val isPaired: Boolean get() = deviceId != null && pinHex != null && host != null

    fun save(deviceId: String, pinHex: String, host: String, pcName: String) {
        prefs.edit()
            .putString(KEY_DEVICE_ID, deviceId)
            .putString(KEY_PIN, pinHex)
            .putString(KEY_HOST, host)
            .putString(KEY_PC_NAME, pcName)
            .apply()
    }

    fun clear() {
        prefs.edit().remove(KEY_DEVICE_ID).remove(KEY_PIN).remove(KEY_HOST).remove(KEY_PC_NAME).apply()
    }

    companion object {
        const val PREFS = "phonemic"
        private const val KEY_DEVICE_ID = "deviceId"
        private const val KEY_PIN = "pinHex"
        private const val KEY_HOST = "host"
        private const val KEY_PC_NAME = "pcName"
    }
}
