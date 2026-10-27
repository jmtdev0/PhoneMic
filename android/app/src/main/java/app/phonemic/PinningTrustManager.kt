package app.phonemic

import android.content.SharedPreferences
import java.security.MessageDigest
import java.security.cert.CertificateException
import java.security.cert.X509Certificate
import javax.net.ssl.X509TrustManager

/**
 * Trust-on-first-use pinning for the PC's self-signed certificate.
 *
 * The first certificate seen for a host is remembered; afterwards only that exact certificate is accepted.
 * The SHA-256 is shown on screen and printed by the PC, so it can be compared by eye.
 */
class PinningTrustManager(
    private val prefs: SharedPreferences,
    private val host: String,
    private val onSeen: (String) -> Unit,
) : X509TrustManager {

    override fun checkClientTrusted(chain: Array<X509Certificate>, authType: String) {
        throw CertificateException("Client certificates are not supported")
    }

    override fun checkServerTrusted(chain: Array<X509Certificate>, authType: String) {
        val seen = sha256(chain[0])
        onSeen(seen)
        val pinned = prefs.getString(pinKey(host), null)
        if (pinned == null) {
            prefs.edit().putString(pinKey(host), seen).apply()
        } else if (pinned != seen) {
            throw CertificateException("certificate changed")
        }
    }

    override fun getAcceptedIssuers(): Array<X509Certificate> = emptyArray()

    companion object {
        fun pinKey(host: String) = "pin:$host"

        fun sha256(cert: X509Certificate): String =
            MessageDigest.getInstance("SHA-256").digest(cert.encoded).joinToString(":") { "%02X".format(it) }
    }
}
