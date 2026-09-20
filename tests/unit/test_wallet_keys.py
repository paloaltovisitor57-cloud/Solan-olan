"""Hot wallet key file: generation, permissions, formats, signing, and secret hygiene."""

from __future__ import annotations

import base64
import json
import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

from solana_sniper.telemetry.redaction import registry, scrub_text
from solana_sniper.wallet import keys
from solana_sniper.wallet.keys import (
    HotWallet,
    WalletError,
    WalletFileError,
    generate,
    import_file,
    load,
    parse_secret,
    public_key_of,
)


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    registry.clear()
    yield
    registry.clear()


_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(data: bytes) -> str:
    num = int.from_bytes(data, "big")
    out = ""
    while num:
        num, rem = divmod(num, 58)
        out = _ALPHABET[rem] + out
    return "1" * (len(data) - len(data.lstrip(b"\0"))) + out


def unsigned_transfer(payer: Pubkey, *, signers: int = 1) -> str:
    """A serialised unsigned v0 transaction whose fee payer is `payer`."""
    ixs = [transfer(TransferParams(from_pubkey=payer, to_pubkey=Pubkey.default(), lamports=1))]
    if signers == 2:
        other = Keypair().pubkey()
        ixs.append(transfer(TransferParams(from_pubkey=other, to_pubkey=payer, lamports=1)))
    msg = MessageV0.try_compile(payer, ixs, [], Hash.default())
    tx = VersionedTransaction.populate(msg, [Signature.default()] * signers)
    return base64.b64encode(bytes(tx)).decode()


def test_generate_writes_a_private_key_file_and_loads_it_back(tmp_path: Path) -> None:
    path = tmp_path / "wallet" / "hot-wallet.json"
    wallet = generate(path)
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    data = json.loads(path.read_text())
    assert isinstance(data, list) and len(data) == 64 and all(0 <= b <= 255 for b in data)
    again = load(path)
    assert again.pubkey == wallet.pubkey and len(wallet.pubkey) in range(32, 45)
    assert public_key_of(path) == wallet.pubkey
    with pytest.raises(WalletFileError, match="refusing to overwrite"):
        generate(path)


def test_repr_and_logs_never_expose_the_secret(tmp_path: Path) -> None:
    path = tmp_path / "hot-wallet.json"
    wallet = generate(path)
    secret_json = path.read_text().strip()
    secret_b58 = str(Keypair.from_bytes(bytes(json.loads(secret_json))))
    text = repr(wallet) + str(wallet) + f"{wallet}"
    assert wallet.pubkey in text
    assert secret_b58 not in text and secret_json not in text
    assert not hasattr(wallet, "__dict__")  # slots only: nothing to dump accidentally
    # the redaction registry masks every textual form of the secret
    assert scrub_text(f"key={secret_b58}") == "key=***"
    assert scrub_text(secret_json) == "***"
    assert scrub_text(json.dumps(json.loads(secret_json))) == "***"


def test_load_refuses_unsafe_files(tmp_path: Path) -> None:
    path = tmp_path / "hot-wallet.json"
    with pytest.raises(WalletFileError, match="wallet create"):
        load(path)
    generate(path)
    os.chmod(path, 0o644)
    with pytest.raises(WalletFileError, match="chmod 600"):
        load(path)
    os.chmod(path, 0o600)
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(WalletFileError, match="symlink"):
        load(link)
    directory = tmp_path / "dir.json"
    directory.mkdir()
    with pytest.raises(WalletFileError, match="not a regular file"):
        load(directory)


def test_parse_accepts_keypair_formats_and_refuses_seed_phrases() -> None:
    kp = Keypair()
    assert parse_secret(json.dumps(list(bytes(kp)))).pubkey() == kp.pubkey()
    assert parse_secret(str(kp)).pubkey() == kp.pubkey()  # base58 64-byte
    assert parse_secret(f'"{kp}"\n').pubkey() == kp.pubkey()
    seed = bytes(kp)[:32]
    assert parse_secret(json.dumps(list(seed))).pubkey() == kp.pubkey()
    with pytest.raises(WalletFileError, match="seed phrases are never accepted"):
        parse_secret("abandon " * 11 + "about")
    with pytest.raises(WalletFileError, match="empty"):
        parse_secret("   ")
    with pytest.raises(WalletFileError, match="JSON array"):
        parse_secret("[1, 2, 3]")
    with pytest.raises(WalletFileError, match="not valid JSON"):
        parse_secret("[1, 2,")
    with pytest.raises(WalletFileError, match="neither"):
        parse_secret("not-base58-0OIl")
    with pytest.raises(WalletFileError, match="must be 64 or 32 bytes"):
        parse_secret(b58encode(bytes(kp)[:48]))
    # a base58 32-byte value is a seed, never rejected on length alone
    assert parse_secret(b58encode(seed)).pubkey() == kp.pubkey()


def test_import_copies_an_exported_key_into_the_protected_location(tmp_path: Path) -> None:
    kp = Keypair()
    exported = tmp_path / "phantom-export.txt"
    exported.write_text(str(kp))  # base58, world-readable export
    os.chmod(exported, 0o644)
    dest = tmp_path / "home" / "wallet" / "hot-wallet.json"
    wallet = import_file(exported, dest)
    assert wallet.pubkey == str(kp.pubkey())
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    assert load(dest).pubkey == wallet.pubkey
    with pytest.raises(WalletFileError, match="already exists"):
        import_file(exported, dest)
    with pytest.raises(WalletFileError, match="cannot read"):
        import_file(tmp_path / "missing.txt", tmp_path / "other.json")


def test_sign_swap_produces_a_verifiable_signature(tmp_path: Path) -> None:
    wallet = generate(tmp_path / "hot-wallet.json")
    unsigned = unsigned_transfer(Pubkey.from_string(wallet.pubkey))
    signed = wallet.sign_swap(unsigned)
    assert signed.signature and signed.transaction_b64 != unsigned
    assert wallet.verify_signature(signed.transaction_b64, signed.signature)
    assert wallet.verify_signature(unsigned, signed.signature)  # same message, same signature
    tx = VersionedTransaction.from_bytes(base64.b64decode(signed.transaction_b64))
    assert str(tx.signatures[0]) == signed.signature
    # deterministic: signing the same message again yields the same signature
    assert wallet.sign_swap(unsigned).signature == signed.signature
    assert not wallet.verify_signature(unsigned, str(Signature.default()))


def test_sign_swap_refuses_foreign_or_multi_signer_transactions(tmp_path: Path) -> None:
    wallet = generate(tmp_path / "hot-wallet.json")
    foreign = unsigned_transfer(Keypair().pubkey())
    with pytest.raises(WalletError, match="fee payer is not this wallet"):
        wallet.sign_swap(foreign)
    two = unsigned_transfer(Pubkey.from_string(wallet.pubkey), signers=2)
    with pytest.raises(WalletError, match="needs 2 signatures"):
        wallet.sign_swap(two)
    with pytest.raises(WalletError, match="cannot decode"):
        wallet.sign_swap("not base64!!")
    with pytest.raises(WalletError, match="cannot decode"):
        wallet.sign_swap(base64.b64encode(b"garbage").decode())


def test_hot_wallet_holds_no_public_attribute_with_the_secret(tmp_path: Path) -> None:
    wallet = generate(tmp_path / "hot-wallet.json")
    public = [n for n in HotWallet.__slots__ if not n.startswith("_")]
    assert public == ["pubkey", "source"]
    assert wallet.source.endswith("hot-wallet.json")
    assert keys.KEY_FILE_NAME == "hot-wallet.json"
