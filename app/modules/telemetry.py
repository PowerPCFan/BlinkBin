import time
from sqlalchemy.orm import Session
from .data_structures import PasteEventRecord, PasteEventWrite, PasteEvent


def record_paste_event(db: Session, event: PasteEventWrite) -> None:
    try:
        db.add(
            PasteEvent(
                action=event.action.value,
                paste_hash=event.paste_hash,
                actor_ip=event.actor_ip,
                paste_owner_ip=event.paste_owner_ip,
                request_method=event.request_method,
                request_path=event.request_path,
                user_agent=event.user_agent,
                metadata_json=event.metadata_json(),
                created_at=time.time(),
            )
        )
        db.commit()
    except Exception:
        db.rollback()


def load_paste_event(event: PasteEvent) -> PasteEventRecord:
    return PasteEventRecord.from_model(event)
