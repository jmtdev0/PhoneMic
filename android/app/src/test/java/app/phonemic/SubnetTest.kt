package app.phonemic

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class SubnetTest {
    private fun ip(a: Int, b: Int, c: Int, d: Int) = byteArrayOf(a.toByte(), b.toByte(), c.toByte(), d.toByte())

    @Test
    fun aSlash24ListsEveryOtherHost() {
        val hosts = Subnet.candidates(ip(10, 73, 41, 38), 24)
        assertEquals(253, hosts.size)
        assertEquals("10.73.41.1", hosts.first())
        assertEquals("10.73.41.254", hosts.last())
        assertFalse("10.73.41.38" in hosts)
        assertFalse("10.73.41.0" in hosts)
        assertFalse("10.73.41.255" in hosts)
    }

    @Test
    fun largerNetworksAreNarrowedToTheSlash24AroundTheOwnAddress() {
        val hosts = Subnet.candidates(ip(10, 0, 5, 200), 16)
        assertEquals(253, hosts.size)
        assertTrue(hosts.all { it.startsWith("10.0.5.") })
    }

    @Test
    fun smallNetworksOnlyListTheirOwnHosts() {
        assertEquals(listOf("10.73.41.9"), Subnet.candidates(ip(10, 73, 41, 10), 30).filter { it.endsWith(".9") })
        val hosts = Subnet.candidates(ip(10, 73, 41, 10), 30) // 10.73.41.8/30: hosts .9 and .10
        assertEquals(listOf("10.73.41.9"), hosts)
    }

    @Test
    fun addressesAboveOneHundredTwentySevenAreNotMangledBySignedBytes() {
        val hosts = Subnet.candidates(ip(172, 16, 200, 130), 24)
        assertEquals("172.16.200.1", hosts.first())
        assertTrue("172.16.200.200" in hosts)
    }
}
