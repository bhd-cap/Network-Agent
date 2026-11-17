"""Secrets at rest, session cookies, token hashing and output redaction."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class SecretBox:
    """Encrypts credentials (device passwords, API keys) before they hit the database.

    The key comes from ``NETAGENT_SECRET_KEY`` or is generated once into the data
    directory. Back the key up with the database - one is useless without the other.
    """

    def __init__(self, data_dir: Path):
        key = os.environ.get("NETAGENT_SECRET_KEY", "").strip()
        key_file = data_dir / "secret.key"
        if not key:
            if key_file.is_file():
                key = key_file.read_text(encoding="utf-8").strip()
            else:
                key = Fernet.generate_key().decode()
                key_file.write_text(key, encoding="utf-8")
                try:
                    key_file.chmod(0o600)
                except OSError:
                    pass
        self._raw = key.encode()
        self._fernet = Fernet(self._raw)

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode() if plaintext else ""

    def decrypt(self, token: str) -> str:
        if not token:
            return ""
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken:
            return ""

    # -- signed session cookies -----------------------------------------
    def sign_session(self, subject: str, ttl_seconds: int = 12 * 3600) -> str:
        expires = int(time.time()) + ttl_seconds
        payload = f"{subject}|{expires}|{secrets.token_hex(8)}"
        sig = hmac.new(self._raw, payload.encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(payload.encode()).decode() + "." + base64.urlsafe_b64encode(sig).decode()

    def verify_session(self, cookie: str) -> str | None:
        try:
            payload_b64, sig_b64 = cookie.split(".", 1)
            payload = base64.urlsafe_b64decode(payload_b64.encode())
            sig = base64.urlsafe_b64decode(sig_b64.encode())
        except Exception:
            return None
        expected = hmac.new(self._raw, payload, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            return None
        try:
            subject, expires, _ = payload.decode().split("|", 2)
        except ValueError:
            return None
        if int(expires) < time.time():
            return None
        return subject


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_api_key() -> tuple[str, str, str]:
    """Returns (full key, display prefix, hash)."""
    key = "na_" + secrets.token_urlsafe(32)
    return key, key[:10], hash_token(key)


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


# ---------------------------------------------------------------------------
# Redaction: device output and configs routinely contain credentials. They are
# masked before any text is handed to a model (local or cloud).
# ---------------------------------------------------------------------------
_REDACTIONS: list[tuple[re.Pattern, str]] = [
    # Cisco / Arista: "enable secret 5 $1$...", "username bob secret 9 ...", "password 7 0822..."
    (re.compile(r"(?im)^(\s*(?:enable|username\s+\S+.*?)\s*(?:secret|password)\s+(?:\d+\s+|sha512\s+)?)(\S+)(.*)$"), r"\1<redacted>\3"),
    (re.compile(r"(?im)^(\s*(?:password|secret)\s+(?:\d+\s+)?)(\S+)\s*$"), r"\1<redacted>"),
    (re.compile(r"(?im)(\bkey(?:-string)?\s+(?:\d+\s+)?)(\S+)\s*$"), r"\1<redacted>"),
    (re.compile(r"(?im)(\b(?:pre-shared-key|psk|shared-secret|auth-key)\b\s+(?:\S+\s+)??(?:\d+\s+)?)(\S+)\s*$"), r"\1<redacted>"),
    (re.compile(r"(?im)(\bsnmp-server\s+community\s+)(\S+)"), r"\1<redacted>"),
    (re.compile(r"(?im)(\bsnmp-server\s+user\s+\S+\s+\S+\s+v3\s+.*?\b(?:auth|priv)\s+\S+\s+)(\S+)"), r"\1<redacted>"),
    (re.compile(r"(?im)(\b(?:tacacs|radius)(?:-server)?\b.*?\bkey\s+(?:\d+\s+)?)(\S+)"), r"\1<redacted>"),
    (re.compile(r"(?im)(\bneighbor\s+\S+\s+password\s+(?:\d+\s+)?)(\S+)"), r"\1<redacted>"),
    # FortiOS: set password ENC xxx / set psksecret ENC xxx / set passwd ...
    (re.compile(r"(?im)^(\s*set\s+(?:password|passwd|psksecret|secret|key|auth-pwd|priv-pwd|private-key|ppk-secret|"
                r"secondary-secret|tertiary-secret|auth-password-l1|auth-password-l2|md5-key|keyring)\s+)(.+)$"),
     r"\1<redacted>"),
    (re.compile(r"(?im)^(\s*set\s+community\s+)(.+)$"), r"\1<redacted>"),
    # PEM blocks
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
     "<redacted private key>"),
]


def redact_secrets(text: str) -> str:
    for pattern, repl in _REDACTIONS:
        text = pattern.sub(repl, text)
    return text
