package app.phonemic

import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import java.security.KeyPairGenerator
import java.security.KeyStore
import java.security.PrivateKey
import java.security.Signature
import java.security.spec.ECGenParameterSpec

/**
 * This phone's identity: a P-256 key pair created inside the Android Keystore. The private key cannot be exported;
 * the PC only ever learns the public half, and the phone proves itself by signing a fresh challenge.
 */
object DeviceKey {
    private const val ALIAS = "phonemic-device"
    private const val PROVIDER = "AndroidKeyStore"
    private const val AUTH_PREFIX = "phonemic-auth-v1|"

    private fun store(): KeyStore = KeyStore.getInstance(PROVIDER).apply { load(null) }

    /** The public key as DER SubjectPublicKeyInfo, creating the key pair on first use. */
    fun publicKeyDer(): ByteArray {
        val keyStore = store()
        if (!keyStore.containsAlias(ALIAS)) {
            KeyPairGenerator.getInstance(KeyProperties.KEY_ALGORITHM_EC, PROVIDER).apply {
                initialize(
                    KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_SIGN)
                        .setAlgorithmParameterSpec(ECGenParameterSpec("secp256r1"))
                        .setDigests(KeyProperties.DIGEST_SHA256)
                        .build(),
                )
            }.generateKeyPair()
        }
        return keyStore.getCertificate(ALIAS).publicKey.encoded
    }

    /** DER ECDSA/SHA-256 signature over the PC's challenge, in the format the PC verifies. */
    fun sign(nonceHex: String): ByteArray {
        val key = store().getKey(ALIAS, null) as PrivateKey
        return Signature.getInstance("SHA256withECDSA").run {
            initSign(key)
            update((AUTH_PREFIX + nonceHex).toByteArray(Charsets.US_ASCII))
            sign()
        }
    }

    fun delete() {
        store().deleteEntry(ALIAS)
    }
}
