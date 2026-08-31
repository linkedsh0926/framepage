from __future__ import annotations

import hashlib
import hmac
import time
from pathlib import Path


def derive_job_token(secret: str, job_id: str) -> str:
    digest = hmac.new(secret.encode(), f"job:{job_id}".encode(), hashlib.sha256).hexdigest()
    return f"fp_{digest}"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def token_matches(token: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_token(token), stored_hash)


def sign_download(secret: str, job_id: str, expires: int) -> str:
    return hmac.new(
        secret.encode(), f"download:{job_id}:{expires}".encode(), hashlib.sha256
    ).hexdigest()


def valid_download_signature(
    secret: str, job_id: str, expires: int, signature: str
) -> bool:
    if expires < int(time.time()):
        return False
    expected = sign_download(secret, job_id, expires)
    return hmac.compare_digest(expected, signature)


def resolve_storage_key(root: Path, key: str) -> Path:
    """Resolve a database object key while preventing traversal outside storage."""
    base = root.resolve()
    candidate = (base / key).resolve()
    if candidate != base and base not in candidate.parents:
        raise ValueError("unsafe artifact path")
    return candidate
