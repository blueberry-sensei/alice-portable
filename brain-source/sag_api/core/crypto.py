"""Mã hoá bí mật ở trạng thái nghỉ (secrets at rest).

API key của model và credential sub-agent được lưu trong bảng `settings`, mà file DB nằm trong thư mục
bind-mount của người dùng. Lưu plaintext nghĩa là bất cứ ai đọc được file đó — hoặc
vô tình đưa nó vào một bản backup / một repo — là thấy nguyên key.

Sơ đồ:

- khoá mã hoá = HKDF-SHA256(``settings.secret_key``, info=``sag.model-config.v1``);
- AES-256-GCM, nonce 12 byte ngẫu nhiên mỗi lần ghi;
- định dạng lưu: ``enc:v1:<base64url(nonce||ciphertext)>``.

Hệ quả phải nói trước: **khoá mã hoá dẫn xuất từ `SAG_SECRET_KEY`.** Đổi hoặc mất
`SAG_SECRET_KEY` là mất toàn bộ key đã lưu — phải nhập lại trên UI (không phải mất dữ liệu
tri thức, chỉ là credential). Vì vậy `decrypt_secret` không làm sập ứng dụng khi giải mã
thất bại: nó trả `None` và ghi log, để người dùng còn vào được UI mà nhập lại.
"""

from __future__ import annotations

import base64
import os

from sag_api.core.logging import get_logger

log = get_logger("crypto")

_PREFIX = "enc:v1:"
_NONCE_BYTES = 12
_INFO = b"sag.model-config.v1"


def _derive_key(secret_key: str) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_INFO).derive(
        secret_key.encode("utf-8")
    )


def is_encrypted(value: str) -> bool:
    return value.startswith(_PREFIX)


def encrypt_secret(plaintext: str, secret_key: str) -> str:
    """Mã hoá một bí mật. Chuỗi rỗng trả về nguyên trạng (nghĩa là "chưa đặt")."""
    if not plaintext:
        return plaintext
    if is_encrypted(plaintext):
        return plaintext

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(_NONCE_BYTES)
    sealed = AESGCM(_derive_key(secret_key)).encrypt(nonce, plaintext.encode("utf-8"), None)
    return _PREFIX + base64.urlsafe_b64encode(nonce + sealed).decode("ascii")


def decrypt_secret(stored: str, secret_key: str) -> str | None:
    """Giải mã. Trả `None` khi không giải được (khoá đã đổi / dữ liệu hỏng).

    Giá trị chưa có tiền tố `enc:v1:` được coi là plaintext cũ và trả về nguyên trạng —
    nhờ đó bản ghi lưu trước khi có mã hoá vẫn dùng được, và lần lưu kế tiếp sẽ mã hoá lại.
    """
    if not stored:
        return stored
    if not is_encrypted(stored):
        return stored

    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        raw = base64.urlsafe_b64decode(stored[len(_PREFIX) :].encode("ascii"))
        nonce, sealed = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
        return AESGCM(_derive_key(secret_key)).decrypt(nonce, sealed, None).decode("utf-8")
    except (InvalidTag, ValueError, TypeError) as error:
        # Nguyên nhân gần như luôn là SAG_SECRET_KEY đã đổi. Không raise: người dùng cần
        # vào được Settings để nhập lại key, chứ không phải nhìn một app 500.
        log.error("Không giải mã được credential đã lưu (SAG_SECRET_KEY đã đổi?): %s", error)
        return None
