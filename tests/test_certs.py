import tempfile
import unittest
from pathlib import Path

from cryptography import x509

from phonemic.certs import ensure_certificate, fingerprint_sha256, local_ips


class CertificateTests(unittest.TestCase):
    def test_creates_a_certificate_covering_the_given_addresses(self):
        with tempfile.TemporaryDirectory() as tmp:
            cert_path, key_path = ensure_certificate(Path(tmp), ["192.168.1.50"])
            cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
            san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            ips = {str(ip) for ip in san.get_values_for_type(x509.IPAddress)}
            self.assertIn("192.168.1.50", ips)
            self.assertIn("127.0.0.1", ips)
            self.assertIn("localhost", san.get_values_for_type(x509.DNSName))
            self.assertTrue(key_path.exists())

    def test_is_reused_when_nothing_changed_and_renewed_when_addresses_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            first, _ = ensure_certificate(Path(tmp), ["192.168.1.50"])
            first_bytes = first.read_bytes()
            second, _ = ensure_certificate(Path(tmp), ["192.168.1.50"])
            self.assertEqual(second.read_bytes(), first_bytes)
            third, _ = ensure_certificate(Path(tmp), ["192.168.1.99"])
            self.assertNotEqual(third.read_bytes(), first_bytes)

    def test_fingerprint_is_colon_separated_uppercase_sha256(self):
        with tempfile.TemporaryDirectory() as tmp:
            cert_path, _ = ensure_certificate(Path(tmp), ["192.168.1.50"])
            parts = fingerprint_sha256(cert_path).split(":")
            self.assertEqual(len(parts), 32)
            self.assertTrue(all(len(part) == 2 and part == part.upper() for part in parts))

    def test_local_ips_never_returns_loopback_or_link_local(self):
        for ip in local_ips():
            self.assertFalse(ip.startswith(("127.", "169.254.")), ip)


if __name__ == "__main__":
    unittest.main()
