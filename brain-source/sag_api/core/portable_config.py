"""Bundle cấu hình portable có credential, mã hoá bằng passphrase của người dùng.

Đây là lớp mã hoá **khác** với ``core.crypto``:

- ``core.crypto`` bảo vệ secret trong DB bằng ``SAG_SECRET_KEY`` riêng của một Brain;
- module này bảo vệ file mang sang project/máy khác bằng passphrase do người dùng nhập.

API chỉ trả ciphertext. Plaintext credential được giải mã và đóng gói bên trong process Brain,
không bao giờ trả về JSON API hay browser.
"""

from __future__ import annotations

import base64
import json
import os
from datetime import UTC, datetime
from typing import Any, Literal

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

PortableConfigKind = Literal["alice-model-config", "alice-sub-agent-config"]

_FORMAT = "alice-portable-config"
_VERSION = 1
_SALT_BYTES = 16
_NONCE_BYTES = 12
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.b64decode(value.encode("ascii"), altchars=b"-_", validate=True)


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P).derive(
        passphrase.encode("utf-8")
    )


def _aad(kind: PortableConfigKind) -> bytes:
    return f"{_FORMAT}:v{_VERSION}:{kind}".encode("ascii")


def seal_portable_config(
    kind: PortableConfigKind,
    config: dict[str, Any],
    passphrase: str,
) -> dict[str, Any]:
    """Mã hoá config thành bundle JSON portable; không chứa plaintext secret."""
    salt = os.urandom(_SALT_BYTES)
    nonce = os.urandom(_NONCE_BYTES)
    plaintext = json.dumps(
        {
            "kind": kind,
            "version": _VERSION,
            "exported_at": datetime.now(UTC).isoformat(),
            "config": config,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    ciphertext = AESGCM(_derive_key(passphrase, salt)).encrypt(
        nonce,
        plaintext,
        _aad(kind),
    )
    return {
        "format": _FORMAT,
        "version": _VERSION,
        "kind": kind,
        "contains_secrets": True,
        "cipher": "AES-256-GCM",
        "kdf": {
            "name": "scrypt",
            "salt": _b64encode(salt),
            "n": _SCRYPT_N,
            "r": _SCRYPT_R,
            "p": _SCRYPT_P,
        },
        "nonce": _b64encode(nonce),
        "ciphertext": _b64encode(ciphertext),
    }


def open_portable_config(
    bundle: dict[str, Any],
    passphrase: str,
    expected_kind: PortableConfigKind,
) -> dict[str, Any]:
    """Giải mã và kiểm tra metadata/AAD. Sai passphrase hoặc bị sửa đều thất bại như nhau."""
    try:
        if (
            bundle.get("format") != _FORMAT
            or bundle.get("version") != _VERSION
            or bundle.get("kind") != expected_kind
            or bundle.get("cipher") != "AES-256-GCM"
        ):
            raise ValueError("unsupported_bundle")
        kdf = bundle.get("kdf")
        if not isinstance(kdf, dict) or (
            kdf.get("name") != "scrypt"
            or kdf.get("n") != _SCRYPT_N
            or kdf.get("r") != _SCRYPT_R
            or kdf.get("p") != _SCRYPT_P
        ):
            raise ValueError("unsupported_kdf")
        salt = _b64decode(str(kdf.get("salt") or ""))
        nonce = _b64decode(str(bundle.get("nonce") or ""))
        ciphertext = _b64decode(str(bundle.get("ciphertext") or ""))
        if len(salt) != _SALT_BYTES or len(nonce) != _NONCE_BYTES or not ciphertext:
            raise ValueError("invalid_bundle")
        plaintext = AESGCM(_derive_key(passphrase, salt)).decrypt(
            nonce,
            ciphertext,
            _aad(expected_kind),
        )
        payload = json.loads(plaintext.decode("utf-8"))
        if (
            not isinstance(payload, dict)
            or payload.get("kind") != expected_kind
            or payload.get("version") != _VERSION
            or not isinstance(payload.get("config"), dict)
        ):
            raise ValueError("invalid_payload")
        return payload["config"]
    except (InvalidTag, ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("portable_config_decryption_failed") from error
