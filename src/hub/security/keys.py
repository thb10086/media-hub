"""API key format: ``mh_<12 hex prefix>_<secret>``.

Only the prefix (for lookup and display) and a SHA-256 digest are stored, so a
key cannot be read back after it has been issued.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Final, NamedTuple

from hub.constants import API_KEY_PREFIX

PREFIX_BYTES: Final = 6
SECRET_BYTES: Final = 32
SEPARATOR: Final = "_"


class GeneratedKey(NamedTuple):
    key: str
    prefix: str
    key_hash: str


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def generate_key() -> GeneratedKey:
    prefix = secrets.token_hex(PREFIX_BYTES)
    key = SEPARATOR.join((API_KEY_PREFIX, prefix, secrets.token_urlsafe(SECRET_BYTES)))
    return GeneratedKey(key=key, prefix=prefix, key_hash=hash_key(key))


def key_prefix(key: str) -> str | None:
    """The lookup prefix of a well-formed key, or None."""
    head, separator, rest = key.partition(SEPARATOR)
    if head != API_KEY_PREFIX or not separator:
        return None
    prefix, separator, secret = rest.partition(SEPARATOR)
    if len(prefix) != PREFIX_BYTES * 2 or not separator or not secret:
        return None
    return prefix


def verify_key(key: str, expected_hash: str) -> bool:
    return hmac.compare_digest(hash_key(key), expected_hash)


def generate_secret() -> str:
    return secrets.token_urlsafe(SECRET_BYTES)
