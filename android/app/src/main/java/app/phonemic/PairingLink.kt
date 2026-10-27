package app.phonemic

import java.net.URI
import java.net.URLDecoder

/**
 * The content of the pairing QR code: `phonemic://pair?v=1&t=<token>&p=<key pin>&h=<host:port>&n=<pc name>`.
 *
 * The link comes from a camera scan, so it is untrusted input: every field is validated before use.
 */
data class PairingLink(val token: String, val pinHex: String, val host: String, val pcName: String) {
    companion object {
        const val MAX_NAME_LENGTH = 40
        private val TOKEN = Regex("^[A-Za-z0-9_-]{8,64}$")
        private val PIN = Regex("^[0-9a-f]{64}$")
        private val HOST = Regex("^[A-Za-z0-9.-]{1,253}:\\d{1,5}$")

        fun parse(link: String?): PairingLink? {
            if (link == null) return null
            val uri = try {
                URI(link)
            } catch (_: Exception) {
                return null
            }
            if (uri.scheme != "phonemic" || uri.authority != "pair") return null
            val params = (uri.rawQuery ?: return null).split("&").mapNotNull { pair ->
                val separator = pair.indexOf('=')
                if (separator < 0) null else decode(pair.substring(0, separator)) to decode(pair.substring(separator + 1))
            }.toMap()
            if (params["v"] != "1") return null
            val token = params["t"]?.takeIf { TOKEN.matches(it) } ?: return null
            val pin = params["p"]?.lowercase()?.takeIf { PIN.matches(it) } ?: return null
            val host = params["h"]?.takeIf { HOST.matches(it) } ?: return null
            if (host.substringAfterLast(':').toInt() !in 1..65535) return null
            val name = (params["n"] ?: "").filter { !it.isISOControl() }.trim().take(MAX_NAME_LENGTH).ifEmpty { "PC" }
            return PairingLink(token, pin, host, name)
        }

        private fun decode(value: String) = URLDecoder.decode(value, "UTF-8")
    }
}
