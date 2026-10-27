"""Paired phones and the one-time tokens used to pair them.

A phone is paired by scanning a QR code that carries a short-lived, single-use token. The phone swaps it for a
permanent identity: it generates a key pair on the device (Android Keystore, never exported) and sends the public key.
Afterwards it proves who it is by signing a fresh challenge, so no secret ever travels and a phone can be revoked.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import re
import secrets
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

AUTH_PREFIX = b"phonemic-auth-v1|"
MAX_NAME_LENGTH = 40
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def clean_name(raw: object) -> str:
    """A display-safe device name: no control characters, trimmed and length-limited."""
    name = _CONTROL_CHARS.sub("", str(raw or "")).strip()[:MAX_NAME_LENGTH]
    return name or "Phone"


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class Device:
    id: str
    name: str
    public_key: str  # base64 of the DER SubjectPublicKeyInfo
    created_at: str
    last_seen: str | None = None
    last_address: str | None = None


class DeviceRegistry:
    """The paired phones, persisted as JSON (in memory only when no path is given)."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._devices: dict[str, Device] = {}
        if path is not None and path.exists():
            for item in json.loads(path.read_text(encoding="utf-8")).get("devices", []):
                device = Device(**item)
                self._devices[device.id] = device

    def list(self) -> list[Device]:
        return sorted(self._devices.values(), key=lambda d: d.created_at)

    def get(self, device_id: str) -> Device | None:
        return self._devices.get(device_id)

    @staticmethod
    def validate_public_key(public_key_der: bytes) -> ec.EllipticCurvePublicKey:
        """Only P-256 keys are accepted; anything else raises ValueError."""
        try:
            key = serialization.load_der_public_key(public_key_der)
        except ValueError as exc:
            raise ValueError("not a DER public key") from exc
        if not isinstance(key, ec.EllipticCurvePublicKey) or key.curve.name != "secp256r1":
            raise ValueError("only P-256 keys are supported")
        return key

    def add(self, name: object, public_key_der: bytes) -> Device:
        self.validate_public_key(public_key_der)
        device = Device(
            id=secrets.token_hex(8),
            name=clean_name(name),
            public_key=base64.b64encode(public_key_der).decode("ascii"),
            created_at=_now(),
        )
        self._devices[device.id] = device
        self._save()
        return device

    def revoke(self, device_id: str) -> bool:
        if self._devices.pop(device_id, None) is None:
            return False
        self._save()
        return True

    def touch(self, device_id: str, address: str) -> None:
        device = self._devices.get(device_id)
        if device is not None:
            device.last_seen = _now()
            device.last_address = address
            self._save()

    def verify(self, device_id: str, nonce_hex: str, signature: bytes) -> bool:
        """True when `signature` (DER ECDSA/SHA-256) was made by the device over the challenge."""
        device = self._devices.get(device_id)
        if device is None:
            return False
        key = self.validate_public_key(base64.b64decode(device.public_key))
        try:
            key.verify(signature, AUTH_PREFIX + nonce_hex.encode("ascii"), ec.ECDSA(hashes.SHA256()))
        except InvalidSignature:
            return False
        return True

    def _save(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"devices": [asdict(d) for d in self.list()]}, indent=2)
        temporary = self._path.with_suffix(".tmp")
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, self._path)


class EnrollmentTokens:
    """At most one valid pairing token at a time: single use, short lived."""

    def __init__(self, ttl_seconds: float = 300, clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._token: str | None = None
        self._expires_at = 0.0

    def create(self) -> str:
        """A new token; any previous one stops being valid."""
        self._token = secrets.token_urlsafe(16)
        self._expires_at = self._clock() + self.ttl_seconds
        return self._token

    def consume(self, candidate: str) -> bool:
        if self._token is None or self._clock() >= self._expires_at:
            self._token = None
            return False
        if not secrets.compare_digest(candidate.encode(), self._token.encode()):
            return False
        self._token = None
        return True
