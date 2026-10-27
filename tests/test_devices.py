import base64
import json
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from phonemic.devices import AUTH_PREFIX, DeviceRegistry, EnrollmentTokens, clean_name


class FakePhone:
    """What the Android app does: a P-256 key pair and ECDSA/SHA-256 signatures over the challenge."""

    def __init__(self) -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())

    @property
    def public_key_der(self) -> bytes:
        return self.key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)

    def sign(self, nonce_hex: str) -> bytes:
        return self.key.sign(AUTH_PREFIX + nonce_hex.encode(), ec.ECDSA(hashes.SHA256()))


class DeviceRegistryTests(unittest.TestCase):
    def test_a_paired_phone_can_prove_who_it_is_and_others_cannot(self):
        registry = DeviceRegistry()
        phone, impostor = FakePhone(), FakePhone()
        device = registry.add("S20 FE", phone.public_key_der)
        self.assertTrue(registry.verify(device.id, "abcd", phone.sign("abcd")))
        self.assertFalse(registry.verify(device.id, "abcd", impostor.sign("abcd")))
        self.assertFalse(registry.verify(device.id, "ffff", phone.sign("abcd")))  # signature for another challenge
        self.assertFalse(registry.verify("unknown", "abcd", phone.sign("abcd")))

    def test_only_p256_keys_are_accepted(self):
        registry = DeviceRegistry()
        rsa_der = rsa.generate_private_key(65537, 2048).public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        p384_der = ec.generate_private_key(ec.SECP384R1()).public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        for bad in (b"garbage", rsa_der, p384_der):
            with self.assertRaises(ValueError):
                registry.add("x", bad)

    def test_revoked_phones_no_longer_verify(self):
        registry = DeviceRegistry()
        phone = FakePhone()
        device = registry.add("S20 FE", phone.public_key_der)
        self.assertTrue(registry.revoke(device.id))
        self.assertFalse(registry.revoke(device.id))
        self.assertFalse(registry.verify(device.id, "abcd", phone.sign("abcd")))

    def test_devices_survive_a_restart_and_the_file_has_no_private_material(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data" / "devices.json"
            phone = FakePhone()
            device = DeviceRegistry(path).add("S20 FE", phone.public_key_der)
            reloaded = DeviceRegistry(path)
            self.assertEqual([d.id for d in reloaded.list()], [device.id])
            self.assertTrue(reloaded.verify(device.id, "abcd", phone.sign("abcd")))
            stored = json.loads(path.read_text(encoding="utf-8"))["devices"][0]
            self.assertEqual(base64.b64decode(stored["public_key"]), phone.public_key_der)
            self.assertNotIn("PRIVATE", path.read_text(encoding="utf-8"))

    def test_touch_records_the_last_connection(self):
        registry = DeviceRegistry()
        device = registry.add("S20 FE", FakePhone().public_key_der)
        registry.touch(device.id, "192.168.1.37")
        self.assertEqual(registry.get(device.id).last_address, "192.168.1.37")
        self.assertIsNotNone(registry.get(device.id).last_seen)

    def test_names_are_made_safe_for_display(self):
        self.assertEqual(clean_name("S20 FE\x00\n de Javier"), "S20 FE de Javier")
        self.assertEqual(clean_name("   "), "Phone")
        self.assertEqual(clean_name(None), "Phone")
        self.assertEqual(len(clean_name("x" * 200)), 40)


class EnrollmentTokenTests(unittest.TestCase):
    def test_a_token_works_once(self):
        tokens = EnrollmentTokens()
        token = tokens.create()
        self.assertTrue(tokens.consume(token))
        self.assertFalse(tokens.consume(token))

    def test_a_token_expires(self):
        now = [0.0]
        tokens = EnrollmentTokens(ttl_seconds=300, clock=lambda: now[0])
        token = tokens.create()
        now[0] = 299.0
        self.assertTrue(tokens.consume(token))
        token = tokens.create()
        now[0] = 700.0
        self.assertFalse(tokens.consume(token))

    def test_a_new_token_cancels_the_previous_one(self):
        tokens = EnrollmentTokens()
        old = tokens.create()
        new = tokens.create()
        self.assertFalse(tokens.consume(old))
        self.assertTrue(tokens.consume(new))

    def test_wrong_tokens_are_rejected_and_do_not_burn_the_real_one(self):
        tokens = EnrollmentTokens()
        token = tokens.create()
        self.assertFalse(tokens.consume("nope"))
        self.assertTrue(tokens.consume(token))


if __name__ == "__main__":
    unittest.main()
