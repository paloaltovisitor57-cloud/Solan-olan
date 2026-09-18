"""Minimal base58 decoding used only to validate the *public* key encoding.

No key generation, signing, or private-key parsing lives anywhere in this project.
"""

from __future__ import annotations

_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_INDEX = {c: i for i, c in enumerate(_ALPHABET)}
SOLANA_PUBLIC_KEY_BYTES = 32


def b58decode(value: str) -> bytes:
    """Decode a base58 string. Raises ValueError on any non-alphabet character."""
    if not value:
        raise ValueError("empty base58 string")
    num = 0
    for ch in value:
        digit = _INDEX.get(ch)
        if digit is None:
            raise ValueError("invalid base58 character")
        num = num * 58 + digit
    raw = num.to_bytes((num.bit_length() + 7) // 8, "big") if num else b""
    leading = len(value) - len(value.lstrip("1"))
    return b"\x00" * leading + raw


def is_solana_public_key(value: str) -> bool:
    """True only for a base58 string that decodes to exactly 32 bytes."""
    if not (32 <= len(value) <= 44):
        return False
    try:
        return len(b58decode(value)) == SOLANA_PUBLIC_KEY_BYTES
    except ValueError:
        return False
