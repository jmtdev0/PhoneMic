"""Self-signed TLS certificate for the local HTTPS server, with a key that never changes.

Chrome only exposes the microphone on secure origins, so the phone page has to be served over HTTPS even on the
local network. The Android app does not trust the certificate itself but the server's public key (its SHA-256 is the
"pin"). The key is created once and kept; when the PC's addresses change only the certificate is re-issued, so a
paired phone keeps recognising the PC.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import socket
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

CERT_VALID_DAYS = 365
RENEW_BEFORE_DAYS = 30


def local_ips() -> list[str]:
    """IPv4 addresses of this PC on its local networks, the default-route one first."""
    primary: str | None = None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("10.255.255.255", 1))  # no packet is sent; picks the outgoing interface
            primary = probe.getsockname()[0]
    except OSError:
        pass
    others: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            others.add(info[4][0])
    except socket.gaierror:
        pass
    usable = [ip for ip in others if not ip.startswith(("127.", "169.254."))]
    return ([primary] if primary and not primary.startswith("127.") else []) + sorted(set(usable) - {primary})


def ensure_certificate(cert_dir: Path, ips: list[str]) -> tuple[Path, Path]:
    """Return (cert, key) paths, re-issuing the certificate when the addresses changed or expiry is near."""
    cert_dir.mkdir(parents=True, exist_ok=True)
    cert_path, key_path, san_path = cert_dir / "cert.pem", cert_dir / "key.pem", cert_dir / "san.txt"
    names = sorted({"localhost", socket.gethostname(), "127.0.0.1", *ips})
    key = _load_key(key_path)
    if key is None:
        key = ec.generate_private_key(ec.SECP256R1())
        key_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
    elif _is_current(cert_path, san_path, names, key):
        return cert_path, key_path

    now = dt.datetime.now(dt.timezone.utc)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "PhoneMic")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=CERT_VALID_DAYS))
        .add_extension(x509.SubjectAlternativeName([_general_name(n) for n in names]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    san_path.write_text("\n".join(names), encoding="utf-8")
    return cert_path, key_path


def key_pin_hex(cert_path: Path) -> str:
    """SHA-256 of the certificate's public key (SubjectPublicKeyInfo), lowercase hex without separators.

    The Android app computes the same value from the server's certificate and compares it with the one it got from
    the pairing QR code.
    """
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    spki = cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    digest = hashes.Hash(hashes.SHA256())
    digest.update(spki)
    return digest.finalize().hex()


def format_pin(pin_hex: str) -> str:
    """AA:BB:CC:... for humans."""
    return ":".join(pin_hex[i : i + 2] for i in range(0, len(pin_hex), 2)).upper()


def _general_name(value: str) -> x509.GeneralName:
    try:
        return x509.IPAddress(ipaddress.ip_address(value))
    except ValueError:
        return x509.DNSName(value)


def _load_key(key_path: Path) -> ec.EllipticCurvePrivateKey | None:
    if not key_path.exists():
        return None
    try:
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    except ValueError:
        return None
    return key if isinstance(key, ec.EllipticCurvePrivateKey) else None


def _spki(public_key) -> bytes:
    return public_key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


def _is_current(cert_path: Path, san_path: Path, names: list[str], key: ec.EllipticCurvePrivateKey) -> bool:
    if not (cert_path.exists() and san_path.exists()):
        return False
    if san_path.read_text(encoding="utf-8").split("\n") != names:
        return False
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    if _spki(cert.public_key()) != _spki(key.public_key()):
        return False
    renew_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=RENEW_BEFORE_DAYS)
    return cert.not_valid_after_utc > renew_at
