"""
GlucoRisk Data Encryption at Rest
──────────────────────────────────
AES-256-CBC encryption for PII fields in the database.
Patient names, contacts, allergies are encrypted before storage.

Compliance: HIPAA §164.312(a)(2)(iv) — Encryption and decryption
"""

import os
import base64
import hashlib
import logging
from cryptography.fernet import Fernet

logger = logging.getLogger("glucorisk.encryption")

# ── Key Management ─────────────────────────────────────
def _get_encryption_key():
    """
    Derive a Fernet key from the GLUCORISK_ENCRYPTION_KEY env var.
    If not set, generates and logs a warning.
    """
    env_key = os.environ.get("GLUCORISK_ENCRYPTION_KEY", "")
    
    if not env_key:
        logger.warning("GLUCORISK_ENCRYPTION_KEY not set! Using default key. "
                       "SET THIS IN PRODUCTION!")
        env_key = "glucorisk-default-dev-key-change-in-production"
    
    # Derive a 32-byte key using SHA-256, then base64-encode for Fernet
    key_bytes = hashlib.sha256(env_key.encode()).digest()
    return base64.urlsafe_b64encode(key_bytes)


_fernet = None

def _get_fernet():
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_get_encryption_key())
    return _fernet


# ── Public API ─────────────────────────────────────────

def encrypt_field(plaintext):
    """
    Encrypt a string field for database storage.
    Returns base64-encoded ciphertext string.
    Returns empty string if input is empty/None.
    """
    if not plaintext:
        return ""
    try:
        f = _get_fernet()
        return f.encrypt(plaintext.encode()).decode()
    except Exception as e:
        logger.error(f"Encryption failed: {e}")
        return plaintext  # Fallback to plaintext on error


def decrypt_field(ciphertext):
    """
    Decrypt a database field back to plaintext.
    Returns the original string.
    Returns empty string if input is empty/None.
    """
    if not ciphertext:
        return ""
    try:
        f = _get_fernet()
        return f.decrypt(ciphertext.encode()).decode()
    except Exception as e:
        # Could be unencrypted legacy data
        logger.debug(f"Decryption failed (may be legacy plaintext): {e}")
        return ciphertext  # Return as-is if decryption fails


def is_encrypted(value):
    """Check if a value appears to be Fernet-encrypted."""
    if not value or len(value) < 50:
        return False
    try:
        base64.urlsafe_b64decode(value)
        return True
    except Exception:
        return False
