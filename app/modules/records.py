from dataclasses import dataclass
from .database import Paste


@dataclass(frozen=True)
class PasteRecord:
    paste_hash: str
    text: str
    created_at: float
    updated_at: float
    owner_ip: str
    edit_token_hash: str

    @classmethod
    def from_model(cls, paste: Paste) -> "PasteRecord":
        return cls(
            paste_hash=paste.paste_hash,
            text=paste.text,
            created_at=paste.created_at,
            updated_at=paste.updated_at or paste.created_at,
            owner_ip=paste.ip,
            edit_token_hash=paste.edit_token_hash,
        )

    def as_response(self) -> dict[str, str | float]:
        return {
            "paste_hash": self.paste_hash,
            "text": self.text,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
