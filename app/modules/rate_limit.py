import threading
import time
from dataclasses import dataclass
from typing import Final

from fastapi import HTTPException
from sqlalchemy import Float, String, and_, create_engine, delete, func, insert, select
from sqlalchemy import Column, MetaData, Table
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

from .database import DATA_DIR, Paste
from .data_structures import Challenge, LimiterBucket
from .proof_of_work import MAX_CHALLENGE_EXPIRY

LIMITS_DATABASE_PATH = DATA_DIR / "limits.db"
LIMITS_DATABASE_URL = f"sqlite:///{LIMITS_DATABASE_PATH}"


@dataclass(frozen=True)
class Rule:
    bucket: "LimiterBucket"
    limit: int
    window_seconds: int
    detail: str


limits_engine: Engine = create_engine(LIMITS_DATABASE_URL, connect_args={"check_same_thread": False})
limits_metadata: MetaData = MetaData()
limiter_events: Table = Table(
    "limiter_events",
    limits_metadata,
    Column("bucket", String, nullable=False),
    Column("key", String, nullable=False),
    Column("ts", Float, nullable=False),
)

CREATE_PASTE_RULE: Final[Rule] = Rule(
    bucket=LimiterBucket.CREATE_PASTE_IP,
    limit=5,
    window_seconds=60,
    detail="Rate limit exceeded",
)
EDIT_RULE: Final[Rule] = Rule(
    bucket=LimiterBucket.EDIT_TOKEN,
    limit=3,
    window_seconds=60,
    detail="Edit rate limit exceeded",
)
DELETE_RULE: Final[Rule] = Rule(
    bucket=LimiterBucket.DELETE_IP,
    limit=30,
    window_seconds=60,
    detail="Delete rate limit exceeded",
)
CHALLENGE_RULE: Final[Rule] = Rule(
    bucket=LimiterBucket.CHALLENGE_ISSUE_IP,
    limit=15,
    window_seconds=60,
    detail="Challenge rate limit exceeded",
)
PASTE_READ_RULE: Final[Rule] = Rule(
    bucket=LimiterBucket.PASTE_READ_IP,
    limit=120,
    window_seconds=60,
    detail="Read rate limit exceeded",
)
PASTE_READ_MISS_RULE: Final[Rule] = Rule(
    bucket=LimiterBucket.PASTE_READ_MISS_IP,
    limit=25,
    window_seconds=60,
    detail="Too many invalid paste lookups",
)
ROOT_MISS_RULE: Final[Rule] = Rule(
    bucket=LimiterBucket.ROOT_MISS_IP,
    limit=45,
    window_seconds=60,
    detail="Too many unknown path lookups",
)
FAILED_TOKEN_RULE: Final[Rule] = Rule(
    bucket=LimiterBucket.FAILED_TOKEN_IP,
    limit=10,
    window_seconds=60,
    detail="Too many invalid token attempts from this IP",
)

# Backward-compatible exports used by tests and other modules.
RATE_LIMIT_COUNT = CREATE_PASTE_RULE.limit
RATE_LIMIT_WINDOW = CREATE_PASTE_RULE.window_seconds
EDIT_RATE_LIMIT_COUNT = EDIT_RULE.limit
EDIT_RATE_LIMIT_WINDOW = EDIT_RULE.window_seconds
DELETE_IP_RATE_LIMIT_COUNT = DELETE_RULE.limit
DELETE_IP_RATE_LIMIT_WINDOW = DELETE_RULE.window_seconds
CHALLENGE_RATE_LIMIT_COUNT = CHALLENGE_RULE.limit
CHALLENGE_RATE_LIMIT_WINDOW = CHALLENGE_RULE.window_seconds
PASTE_READ_RATE_LIMIT_COUNT = PASTE_READ_RULE.limit
PASTE_READ_RATE_LIMIT_WINDOW = PASTE_READ_RULE.window_seconds
PASTE_READ_MISS_RATE_LIMIT_COUNT = PASTE_READ_MISS_RULE.limit
PASTE_READ_MISS_RATE_LIMIT_WINDOW = PASTE_READ_MISS_RULE.window_seconds
ROOT_MISS_RATE_LIMIT_COUNT = ROOT_MISS_RULE.limit
ROOT_MISS_RATE_LIMIT_WINDOW = ROOT_MISS_RULE.window_seconds
FAILED_TOKEN_IP_RATE_LIMIT_COUNT = FAILED_TOKEN_RULE.limit
FAILED_TOKEN_IP_RATE_LIMIT_WINDOW = FAILED_TOKEN_RULE.window_seconds
MAX_ACTIVE_CHALLENGES_PER_IP = 10

_LOCK = threading.Lock()


def init_limit_db() -> None:
    limits_metadata.create_all(bind=limits_engine)


def _consume_limiter(bucket: LimiterBucket, key: str, limit: int, window_seconds: int, detail: str) -> None:
    now = time.time()
    cutoff = now - window_seconds

    with _LOCK:
        with limits_engine.begin() as conn:
            typed_conn: Connection = conn
            typed_conn.execute(delete(limiter_events).where(limiter_events.c.ts < cutoff))
            current = int(typed_conn.execute(
                select(func.count())
                .select_from(limiter_events)
                .where(
                    and_(
                        limiter_events.c.bucket == bucket.value,
                        limiter_events.c.key == key,
                        limiter_events.c.ts > cutoff,
                    )
                )
            ).scalar_one())

            if current >= limit:
                raise HTTPException(status_code=429, detail=detail)

            typed_conn.execute(insert(limiter_events).values(bucket=bucket.value, key=key, ts=now))


def _consume_rule(rule: Rule, key: str) -> None:
    _consume_limiter(
        bucket=rule.bucket,
        key=key,
        limit=rule.limit,
        window_seconds=rule.window_seconds,
        detail=rule.detail,
    )


def check_rate_limit(ip: str, db: Session) -> None:
    cutoff = time.time() - RATE_LIMIT_WINDOW
    user_recent = db.query(Paste).filter(Paste.created_at > cutoff, Paste.ip == ip).count()
    if user_recent >= RATE_LIMIT_COUNT:
        raise HTTPException(status_code=429, detail="Rate limit exceeded")


def check_challenge_rate_limit(ip: str, db: Session) -> None:
    _consume_rule(CHALLENGE_RULE, ip)

    cutoff = time.time() - MAX_CHALLENGE_EXPIRY
    active = (
        db.query(Challenge)
        .filter(Challenge.ip == ip, Challenge.used.is_(False), Challenge.created_at > cutoff)
        .count()
    )
    if active >= MAX_ACTIVE_CHALLENGES_PER_IP:
        raise HTTPException(status_code=429, detail="Too many active challenges")


def check_edit_rate_limit(edit_token_hash: str) -> None:
    _consume_rule(EDIT_RULE, edit_token_hash)


def check_delete_rate_limit(ip: str) -> None:
    _consume_rule(DELETE_RULE, ip)


def record_failed_token_attempt(ip: str) -> None:
    _consume_rule(FAILED_TOKEN_RULE, ip)


def check_paste_read_rate_limit(ip: str) -> None:
    _consume_rule(PASTE_READ_RULE, ip)


def record_paste_read_miss(ip: str) -> None:
    _consume_rule(PASTE_READ_MISS_RULE, ip)


def record_root_miss(ip: str) -> None:
    _consume_rule(ROOT_MISS_RULE, ip)
