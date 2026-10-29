package app.phonemic

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Test

class PairingLinkTest {
    private val pin = "ab".repeat(32)
    private val valid = "phonemic://pair?v=1&t=AbC_123-xyz456789&p=$pin&h=10.73.41.20%3A8443&n=MY-PC"

    @Test
    fun parsesTheLinkTheServerProduces() {
        val link = PairingLink.parse(valid)
        assertNotNull(link)
        assertEquals("AbC_123-xyz456789", link!!.token)
        assertEquals(pin, link.pinHex)
        assertEquals("10.73.41.20:8443", link.host)
        assertEquals("MY-PC", link.pcName)
    }

    @Test
    fun decodesPlusSignsAndPercentEscapesInTheName() {
        assertEquals("Pixel 6", PairingLink.parse(valid.replace("MY-PC", "Pixel+6"))!!.pcName)
        assertEquals("Café PC", PairingLink.parse(valid.replace("MY-PC", "Caf%C3%A9+PC"))!!.pcName)
    }

    @Test
    fun cleansAndLimitsTheName() {
        assertEquals("PC", PairingLink.parse(valid.replace("MY-PC", "%00%0A"))!!.pcName)
        assertEquals(PairingLink.MAX_NAME_LENGTH, PairingLink.parse(valid.replace("MY-PC", "x".repeat(200)))!!.pcName.length)
        assertEquals("PC", PairingLink.parse(valid.replace("&n=MY-PC", ""))!!.pcName)
    }

    @Test
    fun acceptsAnUppercasePin() {
        assertEquals(pin, PairingLink.parse(valid.replace(pin, pin.uppercase()))!!.pinHex)
    }

    @Test
    fun rejectsOtherSchemesHostsAndVersions() {
        assertNull(PairingLink.parse(null))
        assertNull(PairingLink.parse(""))
        assertNull(PairingLink.parse("https://pair?v=1"))
        assertNull(PairingLink.parse(valid.replace("phonemic://", "other://")))
        assertNull(PairingLink.parse(valid.replace("//pair", "//evil")))
        assertNull(PairingLink.parse(valid.replace("v=1", "v=2")))
        assertNull(PairingLink.parse("phonemic://pair"))
    }

    @Test
    fun rejectsMalformedFields() {
        assertNull(PairingLink.parse(valid.replace("t=AbC_123-xyz456789", "t=short")))
        assertNull(PairingLink.parse(valid.replace("t=AbC_123-xyz456789", "t=has%20space%20in%20it")))
        assertNull(PairingLink.parse(valid.replace(pin, "ab".repeat(31))))
        assertNull(PairingLink.parse(valid.replace(pin, "zz".repeat(32))))
        assertNull(PairingLink.parse(valid.replace("10.73.41.20%3A8443", "10.73.41.20")))
        assertNull(PairingLink.parse(valid.replace("10.73.41.20%3A8443", "10.73.41.20%3A99999")))
        assertNull(PairingLink.parse(valid.replace("10.73.41.20%3A8443", "evil.com/x%3A8443")))
        assertNull(PairingLink.parse(valid.replace("&t=AbC_123-xyz456789", "")))
    }
}
