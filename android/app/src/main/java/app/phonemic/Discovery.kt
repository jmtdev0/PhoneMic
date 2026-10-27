package app.phonemic

import android.content.Context
import android.net.ConnectivityManager
import java.net.Inet4Address
import java.net.InetSocketAddress
import java.net.Socket
import java.util.concurrent.Callable
import java.util.concurrent.ExecutorCompletionService
import java.util.concurrent.Executors
import javax.net.ssl.SSLSocket
import javax.net.ssl.SSLSocketFactory

/**
 * Finds the PC again after its IP address changed.
 *
 * It tries the other addresses of the Wi-Fi network and keeps the one whose TLS handshake proves it owns the pinned
 * key. A plain sweep needs no multicast (often dropped by routers) and nothing extra on the PC's firewall.
 */
object Discovery {
    private const val CONNECT_TIMEOUT_MS = 300
    private const val HANDSHAKE_TIMEOUT_MS = 1500
    private const val THREADS = 48

    /** "ip:port" of the PC, or null. Blocking; call from a background thread. */
    fun find(context: Context, port: Int, pinHex: String, cancelled: () -> Boolean): String? {
        val manager = context.getSystemService(ConnectivityManager::class.java)
        val properties = manager.getLinkProperties(manager.activeNetwork ?: return null) ?: return null
        val own = properties.linkAddresses.firstOrNull { it.address is Inet4Address } ?: return null
        val candidates = Subnet.candidates((own.address as Inet4Address).address, own.prefixLength)
        val factory = PinnedTls.sslContext(SpkiPinTrustManager(pinHex)).socketFactory

        val pool = Executors.newFixedThreadPool(THREADS)
        val completion = ExecutorCompletionService<String?>(pool)
        try {
            candidates.forEach { ip -> completion.submit(Callable { probe(ip, port, factory) }) }
            repeat(candidates.size) {
                if (cancelled()) return null
                completion.take().get()?.let { return it }
            }
            return null
        } finally {
            pool.shutdownNow()
        }
    }

    private fun probe(ip: String, port: Int, factory: SSLSocketFactory): String? = try {
        Socket().use { plain ->
            plain.connect(InetSocketAddress(ip, port), CONNECT_TIMEOUT_MS)
            plain.soTimeout = HANDSHAKE_TIMEOUT_MS
            (factory.createSocket(plain, ip, port, false) as SSLSocket).use { it.startHandshake() }
        }
        "$ip:$port"
    } catch (_: Exception) {
        null
    }
}
