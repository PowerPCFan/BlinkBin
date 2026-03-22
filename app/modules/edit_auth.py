import hashlib
import hmac
import secrets


def generate_edit_token() -> str:
    return secrets.token_urlsafe(24)


def hash_edit_token(edit_token: str) -> str:
    return hashlib.sha256(edit_token.encode()).hexdigest()


def verify_edit_token(candidate_token: str, stored_hash: str | None) -> bool:
    if not stored_hash:
        return False

    candidate_hash = hash_edit_token(candidate_token)
    return hmac.compare_digest(candidate_hash, stored_hash)
