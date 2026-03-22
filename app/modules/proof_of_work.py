import hashlib
import secrets
import string
import time
from fastapi import HTTPException
from sqlalchemy.orm import Session
from .database import Paste
from .data_structures import PasteEventAction, Challenge, PasteEvent, DifficultyPolicy, DifficultyTier

CHALLENGE_EXPIRY = 180  # base seconds
EXPIRY_PER_DIFFICULTY_STEP = 45  # extra seconds per difficulty above base
MAX_CHALLENGE_EXPIRY = 420  # hard cap in seconds
BASE_DIFFICULTY = 3
MAX_DIFFICULTY = 10
MAX_DIFFICULTY_INCREASE = MAX_DIFFICULTY - BASE_DIFFICULTY


DIFFICULTY_POLICIES: tuple[DifficultyPolicy, ...] = (
    # Short burst window: intentionally aggressive if someone uploads quickly.
    DifficultyPolicy(
        window_seconds=5 * 60,
        tiers=(
            DifficultyTier(min_pastes=5, target_difficulty=8),
            DifficultyTier(min_pastes=8, target_difficulty=9),
            DifficultyTier(min_pastes=12, target_difficulty=10),
        ),
    ),
    # Medium window: sustained activity still ramps difficulty meaningfully.
    DifficultyPolicy(
        window_seconds=30 * 60,
        tiers=(
            DifficultyTier(min_pastes=5, target_difficulty=6),
            DifficultyTier(min_pastes=10, target_difficulty=7),
            DifficultyTier(min_pastes=20, target_difficulty=8),
        ),
    ),
    # Long window: 5 pastes in 2h bumps baseline 3 -> 4.
    DifficultyPolicy(
        window_seconds=2 * 60 * 60,
        tiers=(
            DifficultyTier(min_pastes=5, target_difficulty=4),
            DifficultyTier(min_pastes=12, target_difficulty=5),
            DifficultyTier(min_pastes=25, target_difficulty=6),
        ),
    ),
)
PASTE_ID_LENGTH = 8
PASTE_ID_ALPHABET = string.ascii_letters + string.digits
CROSS_OWNER_WINDOW_SECONDS = 2 * 60 * 60


def get_difficulty_for_ip(ip: str, db: Session) -> int:
    now = time.time()
    windows = [policy.window_seconds for policy in DIFFICULTY_POLICIES]
    max_window = max(windows)

    recent_pastes = (
        db.query(Paste.created_at)
        .filter(Paste.ip == ip, Paste.created_at > (now - max_window))
        .all()
    )
    timestamps = [row[0] for row in recent_pastes]

    difficulty = BASE_DIFFICULTY
    for policy in DIFFICULTY_POLICIES:
        window_seconds = policy.window_seconds
        tiers = policy.tiers
        count_in_window = sum(1 for ts in timestamps if ts > now - window_seconds)

        policy_difficulty = BASE_DIFFICULTY
        for tier in tiers:
            if count_in_window >= tier.min_pastes:
                policy_difficulty = max(policy_difficulty, tier.target_difficulty)

        difficulty = max(difficulty, policy_difficulty)

    # If an IP performs edit/delete operations on pastes it did not create,
    # add a small extra challenge bump for subsequent uploads.
    recent_cross_owner_actions = (
        db.query(PasteEvent)
        .filter(
            PasteEvent.actor_ip == ip,
            PasteEvent.action.in_((PasteEventAction.EDIT.value, PasteEventAction.DELETE.value)),
            PasteEvent.paste_owner_ip.is_not(None),
            PasteEvent.paste_owner_ip != ip,
            PasteEvent.created_at > (now - CROSS_OWNER_WINDOW_SECONDS),
        )
        .count()
    )

    if recent_cross_owner_actions >= 3:
        difficulty += 2
    elif recent_cross_owner_actions >= 1:
        difficulty += 1

    return min(difficulty, MAX_DIFFICULTY)


def get_expiry_for_difficulty(difficulty: int) -> int:
    extra_levels = max(0, difficulty - BASE_DIFFICULTY)
    expiry = CHALLENGE_EXPIRY + (extra_levels * EXPIRY_PER_DIFFICULTY_STEP)
    return min(MAX_CHALLENGE_EXPIRY, expiry)


def generate_challenge(ip: str, db: Session) -> tuple[str, int]:
    # Keep challenge as full SHA-256 hex for PoW entropy.
    challenge = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
    difficulty = get_difficulty_for_ip(ip, db)
    new_challenge = Challenge(
        challenge=challenge,
        created_at=time.time(),
        difficulty=difficulty,
        used=False,
        ip=ip,
    )
    db.add(new_challenge)
    db.commit()
    return challenge, difficulty


def verify_nonce(challenge: str, nonce: int, ip: str, db: Session) -> tuple[bool, str]:
    ch = db.query(Challenge).filter(Challenge.challenge == challenge).first()

    if not ch:
        return False, "Challenge not found"

    if ch.used:
        return False, "Challenge already used"

    if ch.ip != ip:
        return False, "Challenge not for your IP"

    challenge_difficulty = ch.difficulty if ch.difficulty else BASE_DIFFICULTY
    challenge_expiry = get_expiry_for_difficulty(challenge_difficulty)

    if time.time() - ch.created_at > challenge_expiry:
        db.delete(ch)
        db.commit()
        return False, "Challenge expired"

    h = hashlib.sha256(f"{challenge}{nonce}".encode()).hexdigest()

    if not h.startswith("0" * challenge_difficulty):
        return False, "Invalid nonce"

    ch.used = True
    db.commit()
    return True, h


def generate_paste_id(db: Session) -> str:
    for _ in range(10):
        candidate = "".join(secrets.choice(PASTE_ID_ALPHABET) for _ in range(PASTE_ID_LENGTH))
        if not db.query(Paste).filter(Paste.paste_hash == candidate).first():
            return candidate
    raise HTTPException(status_code=500, detail="Could not allocate paste id")
