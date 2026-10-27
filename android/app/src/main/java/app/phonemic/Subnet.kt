package app.phonemic

/** Addresses worth probing when the PC's IP changed; no Android classes so it can be unit tested. */
object Subnet {
    /**
     * Every other usable address of the network around [own] (network and broadcast addresses excluded).
     * Networks larger than a /24 are narrowed to the /24 around [own], and the prefix is capped at /30.
     */
    fun candidates(own: ByteArray, prefixLength: Int): List<String> {
        require(own.size == 4) { "IPv4 address expected" }
        val prefix = prefixLength.coerceIn(24, 30)
        val ownValue = own.fold(0L) { acc, byte -> (acc shl 8) or (byte.toLong() and 0xFF) }
        val mask = (0xFFFFFFFFL shl (32 - prefix)) and 0xFFFFFFFFL
        val network = ownValue and mask
        val broadcast = network or (mask.inv() and 0xFFFFFFFFL)
        return (network + 1 until broadcast).filter { it != ownValue }.map { value ->
            "${value shr 24 and 0xFF}.${value shr 16 and 0xFF}.${value shr 8 and 0xFF}.${value and 0xFF}"
        }
    }
}
