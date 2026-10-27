package app.phonemic

import android.util.Base64
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit

/** Swaps the QR code's one-time token for a permanent identity: sends this phone's public key to the PC. */
object Enroller {
    class Result(val deviceId: String, val pcName: String)

    class PairingRefused(message: String) : IOException(message)

    /** Blocking; call from a background thread. */
    fun enroll(link: PairingLink, deviceName: String, publicKeyDer: ByteArray): Result {
        val client = PinnedTls.clientBuilder(SpkiPinTrustManager(link.pinHex))
            .callTimeout(15, TimeUnit.SECONDS)
            .build()
        val body = JSONObject()
            .put("token", link.token)
            .put("name", deviceName)
            .put("publicKey", Base64.encodeToString(publicKeyDer, Base64.NO_WRAP))
            .toString()
            .toRequestBody("application/json".toMediaType())
        val request = Request.Builder().url("https://${link.host}/enroll").post(body).build()
        client.newCall(request).execute().use { response ->
            val text = response.body?.string().orEmpty()
            if (!response.isSuccessful) {
                throw PairingRefused(text.ifBlank { "The PC refused the pairing (HTTP ${response.code})." })
            }
            val json = JSONObject(text)
            return Result(json.getString("deviceId"), json.optString("name", link.pcName))
        }
    }
}
