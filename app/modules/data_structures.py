import json
from sqlalchemy import Boolean, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from .database import Base
from typing import NamedTuple, Any
from pydantic import BaseModel
from dataclasses import asdict, dataclass
from enum import Enum


class LimiterBucket(str, Enum):
    CREATE_PASTE_IP = "create-paste-ip"
    EDIT_TOKEN = "edit-token"
    DELETE_IP = "delete-ip"
    CHALLENGE_ISSUE_IP = "challenge-issue-ip"
    PASTE_READ_IP = "paste-read-ip"
    PASTE_READ_MISS_IP = "paste-read-miss-ip"
    ROOT_MISS_IP = "root-miss-ip"
    FAILED_TOKEN_IP = "failed-token-ip"


class PasteEventAction(str, Enum):
    CREATE = "create"
    EDIT = "edit"
    DELETE = "delete"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PasteCreateMetadata:
    paste_url: str
    paste_length: int
    challenge: str
    nonce: int


@dataclass(frozen=True)
class PasteEditMetadata:
    paste_url: str
    paste_length: int


@dataclass(frozen=True)
class PasteDeleteMetadata:
    paste_url: str


@dataclass(frozen=True)
class PasteEventWrite:
    action: PasteEventAction
    paste_hash: str
    actor_ip: str
    request_method: str
    request_path: str
    user_agent: str | None
    paste_owner_ip: str | None
    metadata: (
        dict[str, Any]
        | PasteCreateMetadata
        | PasteEditMetadata
        | PasteDeleteMetadata
        | None
    ) = None

    def metadata_json(self) -> str | None:
        if self.metadata is None:
            return None
        if isinstance(self.metadata, dict):
            payload = self.metadata
        elif hasattr(self.metadata, "__dataclass_fields__"):
            payload = asdict(self.metadata)
        else:
            payload = {"value": str(self.metadata)}
        return json.dumps(payload, separators=(",", ":"))


@dataclass(frozen=True)
class PasteEventRecord:
    id: int
    action: PasteEventAction
    paste_hash: str
    actor_ip: str
    paste_owner_ip: str | None
    request_method: str
    request_path: str
    user_agent: str | None
    metadata: dict[str, Any]
    created_at: float

    @classmethod
    def from_model(cls, event: "PasteEvent") -> "PasteEventRecord":
        try:
            metadata = json.loads(event.metadata_json) if event.metadata_json else {}
        except json.JSONDecodeError:
            metadata = {}
        return cls(
            id=event.id,
            action=PasteEventAction(event.action),
            paste_hash=event.paste_hash,
            actor_ip=event.actor_ip,
            paste_owner_ip=event.paste_owner_ip,
            request_method=event.request_method,
            request_path=event.request_path,
            user_agent=event.user_agent,
            metadata=metadata,
            created_at=event.created_at,
        )


class PasteRequest(BaseModel):
    paste: str
    challenge: str
    nonce: int


class ChallengeResponse(BaseModel):
    challenge: str
    difficulty: int
    expires_in: int


class PasteCreateResponse(BaseModel):
    paste_hash: str
    edit_token: str


class PasteReadResponse(BaseModel):
    paste_hash: str
    text: str
    created_at: float
    updated_at: float


class PasteUpdateRequest(BaseModel):
    paste: str
    edit_token: str


class PasteDeleteRequest(BaseModel):
    edit_token: str


class PasteDeleteResponse(BaseModel):
    deleted: bool
    paste_hash: str


class Challenge(Base):
    __tablename__ = "challenges"

    challenge: Mapped[str] = mapped_column(String, primary_key=True, index=True)
    created_at: Mapped[float] = mapped_column(Float)
    difficulty: Mapped[int] = mapped_column(Integer, default=4)
    used: Mapped[bool] = mapped_column(Boolean, default=False)
    ip: Mapped[str] = mapped_column(String)


class PasteEvent(Base):
    __tablename__ = "paste_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    action: Mapped[str] = mapped_column(String, index=True)
    paste_hash: Mapped[str] = mapped_column(String, index=True)
    actor_ip: Mapped[str] = mapped_column(String, index=True)
    paste_owner_ip: Mapped[str | None] = mapped_column(String, nullable=True)
    request_method: Mapped[str] = mapped_column(String)
    request_path: Mapped[str] = mapped_column(String)
    user_agent: Mapped[str | None] = mapped_column(String, nullable=True)
    metadata_json: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, index=True)


class PasteViewStat(Base):
    __tablename__ = "paste_view_stats"

    paste_hash: Mapped[str] = mapped_column(String, primary_key=True, index=True)
    view_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[float] = mapped_column(Float, index=True)


class DifficultyTier(NamedTuple):
    min_pastes: int
    target_difficulty: int


class DifficultyPolicy(NamedTuple):
    window_seconds: int
    tiers: tuple[DifficultyTier, ...]
