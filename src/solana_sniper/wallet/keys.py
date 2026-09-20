"""Hot-wallet key file: generate, import, load, sign.

The key lives in ONE file under the runtime home (`<home>/wallet/hot-wallet.json`, Solana CLI
JSON format: a 64-byte array), directory 0700, file 0600, owned by the user. Anything else is
refused at load time. The secret is held only inside `HotWallet` and is registered with the log
redaction registry so that an accidental log line masks it. `HotWallet.__repr__` shows the
public key only; nothing here ever writes the secret anywhere but the key file.

Accepted key formats when importing: a JSON array of 64 integers (Solana CLI / most wallet
"export private key" files) or a base58 string of the 64-byte keypair or the 32-byte seed. Seed
phrases (mnemonics) are never accepted: export a keypair file from your wallet instead.
"""

from __future__ import annotations

import base64
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from solders.keypair import Keypair
from solders.message import to_bytes_versioned
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from solana_sniper.config.base58 import b58decode
from solana_sniper.telemetry.redaction import register_secret

KEY_FILE_NAME = "hot-wallet.json"
SECRET_BYTES = 64
SEED_BYTES = 32


class WalletError(Exception):
    """Key file or signing problem. Messages never include key material."""


class WalletFileError(WalletError):
    """The key file is missing, unreadable, badly protected or not a keypair."""


@dataclass(frozen=True, slots=True)
class SignedSwap:
    transaction_b64: str
    signature: str  # base58; known before the transaction is sent


class HotWallet:
    """A loaded keypair. The secret never leaves this object; only `pubkey` is public."""

    __slots__ = ("_kp", "pubkey", "source")

    def __init__(self, keypair: Keypair, *, source: str) -> None:
        self._kp = keypair
        self.pubkey = str(keypair.pubkey())
        self.source = source
        # defence in depth: whatever form the secret could take in a stray log line is masked
        register_secret(str(keypair))
        raw = list(bytes(keypair))
        register_secret(json.dumps(raw))
        register_secret(json.dumps(raw, separators=(",", ":")))

    def __repr__(self) -> str:
        return f"HotWallet({self.pubkey})"

    __str__ = __repr__

    def sign_swap(self, transaction_b64: str) -> SignedSwap:
        """Sign a serialised (unsigned) versioned transaction whose fee payer is this wallet.

        The signature is deterministic for the message, so it identifies the transaction on
        chain before anything is broadcast (crash safety relies on this)."""
        try:
            raw = base64.b64decode(transaction_b64, validate=True)
            tx = VersionedTransaction.from_bytes(raw)
        except Exception as exc:
            raise WalletError(f"cannot decode transaction: {type(exc).__name__}") from None
        keys = tx.message.account_keys
        if not keys or str(keys[0]) != self.pubkey:
            raise WalletError("transaction fee payer is not this wallet; refusing to sign")
        required = tx.message.header.num_required_signatures
        if required != 1:
            raise WalletError(
                f"transaction needs {required} signatures; this wallet signs only single-signer "
                "swaps"
            )
        try:
            signed = VersionedTransaction(tx.message, [self._kp])
        except Exception as exc:
            raise WalletError(f"signing failed: {type(exc).__name__}") from None
        return SignedSwap(
            transaction_b64=base64.b64encode(bytes(signed)).decode("ascii"),
            signature=str(signed.signatures[0]),
        )

    def verify_signature(self, transaction_b64: str, signature: str) -> bool:
        """True when `signature` is this wallet's signature over the transaction's message."""
        try:
            tx = VersionedTransaction.from_bytes(base64.b64decode(transaction_b64))
            return bool(
                Signature.from_string(signature).verify(
                    self._kp.pubkey(), to_bytes_versioned(tx.message)
                )
            )
        except Exception:
            return False


# ------------------------------------------------------------------ parsing


def _looks_like_seed_phrase(text: str) -> bool:
    words = text.split()
    return len(words) >= 12 and all(w.isalpha() for w in words)


def parse_secret(text: str) -> Keypair:
    """Parse a key file's content. Never logs or echoes it."""
    stripped = text.strip()
    if not stripped:
        raise WalletFileError("key file is empty")
    if _looks_like_seed_phrase(stripped):
        raise WalletFileError(
            "seed phrases are never accepted; export the keypair file from your wallet "
            "(JSON array or base58) or run `solana-sniper wallet create`"
        )
    if stripped.startswith("["):
        try:
            values = json.loads(stripped)
        except ValueError:
            raise WalletFileError("key file is not valid JSON") from None
        if (
            not isinstance(values, list)
            or len(values) not in (SECRET_BYTES, SEED_BYTES)
            or not all(isinstance(v, int) and 0 <= v <= 255 for v in values)
        ):
            raise WalletFileError(
                f"key file must be a JSON array of {SECRET_BYTES} (or {SEED_BYTES}) bytes"
            )
        data = bytes(values)
    else:
        token = stripped.strip('"')
        try:
            data = b58decode(token)
        except ValueError:
            raise WalletFileError("key file is neither a JSON byte array nor base58") from None
    try:
        if len(data) == SECRET_BYTES:
            return Keypair.from_bytes(data)
        if len(data) == SEED_BYTES:
            return Keypair.from_seed(data)
    except Exception:
        raise WalletFileError("key bytes do not form a valid Ed25519 keypair") from None
    raise WalletFileError(f"key must be {SECRET_BYTES} or {SEED_BYTES} bytes, got {len(data)}")


# ------------------------------------------------------------------ files


def _check_permissions(path: Path) -> None:
    try:
        st = path.lstat()
    except FileNotFoundError:
        raise WalletFileError(
            f"no key file at {path}; run `solana-sniper wallet create` (or `wallet import`)"
        ) from None
    if stat.S_ISLNK(st.st_mode):
        raise WalletFileError(f"{path} is a symlink; the key file must be a regular file")
    if not stat.S_ISREG(st.st_mode):
        raise WalletFileError(f"{path} is not a regular file")
    if os.name == "posix":
        if st.st_uid != os.getuid():
            raise WalletFileError(f"{path} is not owned by the current user")
        if st.st_mode & 0o077:
            raise WalletFileError(
                f"{path} is readable by other users; fix with: chmod 600 '{path}'"
            )


def _write_secret(path: Path, keypair: Keypair) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(path.parent, 0o700)
    payload = json.dumps(list(bytes(keypair)), separators=(",", ":")) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    if os.name == "posix":
        os.chmod(path, 0o600)


def generate(path: Path) -> HotWallet:
    """Create a brand-new hot wallet key file (refuses to overwrite an existing one)."""
    if path.exists():
        raise WalletFileError(
            f"{path} already exists; refusing to overwrite a wallet that may hold funds"
        )
    keypair = Keypair()
    _write_secret(path, keypair)
    return HotWallet(keypair, source=str(path))


def load(path: Path) -> HotWallet:
    """Load the hot wallet, refusing anything that is not a private, regular, 0600 file."""
    _check_permissions(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise WalletFileError(f"cannot read {path}: {type(exc).__name__}") from None
    return HotWallet(parse_secret(text), source=str(path))


def import_file(source: Path, destination: Path) -> HotWallet:
    """Copy an exported key (JSON array or base58) into the protected key file location."""
    if destination.exists():
        raise WalletFileError(f"{destination} already exists; refusing to overwrite")
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise WalletFileError(f"cannot read {source}: {type(exc).__name__}") from None
    keypair = parse_secret(text)
    _write_secret(destination, keypair)
    return HotWallet(keypair, source=str(destination))


def public_key_of(path: Path) -> str:
    return load(path).pubkey
