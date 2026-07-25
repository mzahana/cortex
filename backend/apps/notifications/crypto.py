"""Symmetric encryption for `EmailSettings.api_key_encrypted` (per-tenant
Brevo API keys stored at rest, configurable from the UI instead of only via
env vars).

Uses `cryptography.fernet.Fernet` keyed by `settings.EMAIL_SETTINGS_ENCRYPTION_KEY`
(a base64 urlsafe 32-byte key, env-only per 12-factor -- generate one with
`Fernet.generate_key()`). Never logs the raw key or the encrypted token's
plaintext; callers (`apps.notifications.serializers.EmailSettingsSerializer`)
are responsible for keeping the decrypted value out of API responses/audit
entries.
"""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


def _get_fernet():
    from cryptography.fernet import Fernet

    key = getattr(settings, "EMAIL_SETTINGS_ENCRYPTION_KEY", "") or ""
    if not key:
        raise ImproperlyConfigured(
            "EMAIL_SETTINGS_ENCRYPTION_KEY is not set -- cannot encrypt/decrypt "
            "EmailSettings.api_key_encrypted. Generate one with "
            "`Fernet.generate_key()` and set it in the environment."
        )
    return Fernet(key.encode("utf-8") if isinstance(key, str) else key)


def encrypt_api_key(raw: str) -> bytes:
    """Encrypt `raw` for storage in `EmailSettings.api_key_encrypted`.

    An empty/blank `raw` (the "no key configured"/"key cleared" case) is
    stored as `b""` directly -- never passed to Fernet, which would raise on
    an empty token as well as being pointless ciphertext for "nothing".
    """
    if not raw:
        return b""
    return _get_fernet().encrypt(raw.encode("utf-8"))


def decrypt_api_key(token: bytes) -> str:
    """Decrypt a value previously produced by `encrypt_api_key`.

    An empty/blank `token` (no key configured) decrypts to `""` -- the
    inverse of `encrypt_api_key`'s empty-input handling, never passed to
    Fernet.
    """
    if not token:
        return ""
    return _get_fernet().decrypt(bytes(token)).decode("utf-8")
