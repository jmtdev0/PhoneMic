import hashlib
import tempfile
import unittest
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization

from phonemic.certs import ensure_certificate, format_pin, key_pin_hex, local_ips


class CertificateTests(unittest.TestCase):
    def test_creates_a_certificate_covering_the_given_addresses(self):
        with tempfile.TemporaryDirectory() as tmp:
            cert_path, key_path = ensure_certificate(Path(tmp), ["10.73.41.50"])
            cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
            san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            ips = {str(ip) for ip in san.get_values_for_type(x509.IPAddress)}
            self.assertIn("10.73.41.50", ips)
            self.assertIn("127.0.0.1", ips)
            self.assertIn("localhost", san.get_values_for_type(x509.DNSName))
            self.assertTrue(key_path.exists())

    def test_is_reused_when_nothing_changed_and_renewed_when_addresses_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            first, _ = ensure_certificate(Path(tmp), ["10.73.41.50"])
            first_bytes = first.read_bytes()
            second, _ = ensure_certificate(Path(tmp), ["10.73.41.50"])
            self.assertEqual(second.read_bytes(), first_bytes)
            third, _ = ensure_certificate(Path(tmp), ["10.73.41.99"])
            self.assertNotEqual(third.read_bytes(), first_bytes)

    def test_the_key_and_its_pin_survive_a_change_of_addresses(self):
        with tempfile.TemporaryDirectory() as tmp:
            cert_path, key_path = ensure_certificate(Path(tmp), ["10.73.41.50"])
            key_before, pin_before, cert_before = key_path.read_bytes(), key_pin_hex(cert_path), cert_path.read_bytes()
            ensure_certificate(Path(tmp), ["10.73.41.99"])  # the PC got another address
            self.assertNotEqual(cert_path.read_bytes(), cert_before)  # a new certificate...
            self.assertEqual(key_path.read_bytes(), key_before)  # ...with the same key...
            self.assertEqual(key_pin_hex(cert_path), pin_before)  # ...so paired phones still recognise the PC

    def test_a_replaced_key_gets_a_matching_certificate(self):
        with tempfile.TemporaryDirectory() as tmp:
            cert_path, key_path = ensure_certificate(Path(tmp), ["10.73.41.50"])
            pin_before = key_pin_hex(cert_path)
            key_path.unlink()
            cert_path, _ = ensure_certificate(Path(tmp), ["10.73.41.50"])
            self.assertNotEqual(key_pin_hex(cert_path), pin_before)

    def test_pin_is_the_sha256_of_the_public_key_and_formats_for_humans(self):
        with tempfile.TemporaryDirectory() as tmp:
            cert_path, _ = ensure_certificate(Path(tmp), ["10.73.41.50"])
            pin = key_pin_hex(cert_path)
            self.assertRegex(pin, r"^[0-9a-f]{64}$")
            spki = x509.load_pem_x509_certificate(cert_path.read_bytes()).public_key().public_bytes(
                serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
            )
            self.assertEqual(pin, hashlib.sha256(spki).hexdigest())
            self.assertEqual(format_pin("abcd"), "AB:CD")

    def test_local_ips_never_returns_loopback_or_link_local(self):
        for ip in local_ips():
            self.assertFalse(ip.startswith(("127.", "169.254.")), ip)


if __name__ == "__main__":
    unittest.main()
