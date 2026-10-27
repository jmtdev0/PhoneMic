package app.phonemic

import okhttp3.OkHttpClient
import java.security.MessageDigest
import java.security.cert.CertificateException
import java.security.cert.X509Certificate
import javax.net.ssl.SSLContext
import javax.net.ssl.X509TrustManager

/**
 * Trusts one server identity: the SHA-256 of its public key (SubjectPublicKeyInfo), learned from the pairing QR code.
 *
 * Because the PC keeps the same key when it re-issues its certificate, the pin survives address changes. Proving
 * possession of the matching private key is part of every TLS handshake, so no certificate chain is needed.
 */
class SpkiPinTrustManager(
    private val expectedPinHex: String,
    private val onSeen: (String) -> Unit = {},
) : X509TrustManager {

    override fun checkClientTrusted(chain: Array<X509Certificate>, authType: String) {
        throw CertificateException("Client certificates are not supported")
    }

    override fun checkServerTrusted(chain: Array<X509Certificate>, authType: String) {
        val seen = spkiPinHex(chain[0])
        onSeen(seen)
        if (!MessageDigest.isEqual(seen.toByteArray(), expectedPinHex.toByteArray())) {
            throw CertificateException(MISMATCH_MESSAGE)
        }
    }

    override fun getAcceptedIssuers(): Array<X509Certificate> = emptyArray()

    companion object {
        const val MISMATCH_MESSAGE = "server identity does not match"
    }
}

fun spkiPinHex(certificate: X509Certificate): String =
    MessageDigest.getInstance("SHA-256").digest(certificate.publicKey.encoded).joinToString("") { "%02x".format(it) }

/** AA:BB:CC... for humans, the same format the PC prints. */
fun formatPin(pinHex: String): String = pinHex.chunked(2).joinToString(":") { it.uppercase() }

object PinnedTls {
    fun sslContext(trust: X509TrustManager): SSLContext =
        SSLContext.getInstance("TLS").apply { init(null, arrayOf(trust), null) }

    /** An OkHttp client that only talks to the server owning the pinned key. */
    fun clientBuilder(trust: X509TrustManager): OkHttpClient.Builder =
        OkHttpClient.Builder()
            .sslSocketFactory(sslContext(trust).socketFactory, trust)
            // The pinned key is the authentication; the certificate's addresses may be out of date.
            .hostnameVerifier { _, _ -> true }
}
