"""Per-meeting audio encryption (spec §10, §11).

Audio archive files are AES-256-GCM encrypted with a random per-meeting data key. The key lives in the
OS credential store via ``keyring`` (never in SQLite). When ``cryptography`` or a usable keyring
backend is missing, enabling audio retention is refused.
"""

from __future__ import annotations

import base64
import os
import threading
from abc import ABC, abstractmethod

SERVICE = "sekretaer"
MAGIC = b"SKA1"


class SecretStore(ABC):
    @abstractmethod
    def available(self) -> tuple[bool, str | None]: ...

    @abstractmethod
    def get(self, name: str) -> bytes | None: ...

    @abstractmethod
    def set(self, name: str, value: bytes) -> None: ...

    @abstractmethod
    def delete(self, name: str) -> bool: ...


class KeyringSecretStore(SecretStore):
    def _kr(self):  # noqa: ANN202
        import keyring  # optional dependency

        return keyring

    def available(self) -> tuple[bool, str | None]:
        try:
            kr = self._kr()
            backend = kr.get_keyring()
        except Exception as exc:  # noqa: BLE001
            return False, f"keyring unavailable ({type(exc).__name__})"
        name = type(backend).__name__.lower()
        if "fail" in name or "null" in name or getattr(backend, "priority", 1) <= 0:
            return False, "no usable OS credential store backend"
        return True, None

    def get(self, name: str) -> bytes | None:
        value = self._kr().get_password(SERVICE, name)
        return None if value is None else base64.b64decode(value)

    def set(self, name: str, value: bytes) -> None:
        self._kr().set_password(SERVICE, name, base64.b64encode(value).decode("ascii"))

    def delete(self, name: str) -> bool:
        kr = self._kr()
        try:
            kr.delete_password(SERVICE, name)
            return True
        except Exception:  # noqa: BLE001 - PasswordDeleteError when absent
            return False


class MemorySecretStore(SecretStore):
    """In-process store for tests; never used by the real app."""

    def __init__(self, usable: bool = True) -> None:
        self.usable = usable
        self.values: dict[str, bytes] = {}
        self._lock = threading.Lock()

    def available(self) -> tuple[bool, str | None]:
        return (True, None) if self.usable else (False, "memory store disabled")

    def get(self, name: str) -> bytes | None:
        with self._lock:
            return self.values.get(name)

    def set(self, name: str, value: bytes) -> None:
        with self._lock:
            self.values[name] = value

    def delete(self, name: str) -> bool:
        with self._lock:
            return self.values.pop(name, None) is not None


def _key_name(meeting_id: str) -> str:
    return f"meeting-audio:{meeting_id}"


class AudioCipher:
    def __init__(self, store: SecretStore) -> None:
        self.store = store

    def available(self) -> tuple[bool, str | None]:
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
        except Exception as exc:  # noqa: BLE001
            return False, f"cryptography unavailable ({type(exc).__name__})"
        return self.store.available()

    def _key(self, meeting_id: str, create: bool) -> bytes | None:
        key = self.store.get(_key_name(meeting_id))
        if key is None and create:
            key = os.urandom(32)
            self.store.set(_key_name(meeting_id), key)
        return key

    def encrypt(self, meeting_id: str, data: bytes) -> bytes:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        key = self._key(meeting_id, create=True)
        assert key is not None
        nonce = os.urandom(12)
        return MAGIC + nonce + AESGCM(key).encrypt(nonce, data, meeting_id.encode("ascii"))

    def decrypt(self, meeting_id: str, blob: bytes) -> bytes:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        if not blob.startswith(MAGIC):
            raise ValueError("not an archive blob")
        key = self._key(meeting_id, create=False)
        if key is None:
            raise KeyError("key destroyed")
        nonce, ct = blob[4:16], blob[16:]
        return AESGCM(key).decrypt(nonce, ct, meeting_id.encode("ascii"))

    def destroy_key(self, meeting_id: str) -> bool:
        return self.store.delete(_key_name(meeting_id))
