import threading
import time
from sqlalchemy.orm import Session
from .data_structures import PasteViewStat

VIEW_DEDUP_WINDOW_SECONDS = 30

_recent_views: dict[tuple[str, str], float] = {}
_view_lock = threading.Lock()


def _should_count_view(paste_hash: str, ip: str) -> bool:
    now = time.time()
    key = (paste_hash, ip)

    with _view_lock:
        cutoff = now - VIEW_DEDUP_WINDOW_SECONDS
        expired_keys = [k for k, ts in _recent_views.items() if ts < cutoff]

        for expired_key in expired_keys:
            _recent_views.pop(expired_key, None)

        last_seen = _recent_views.get(key)

        if last_seen is not None and (now - last_seen) < VIEW_DEDUP_WINDOW_SECONDS:
            return False

        _recent_views[key] = now
        return True


def record_paste_view(db: Session, paste_hash: str, ip: str) -> bool:
    if not _should_count_view(paste_hash, ip):
        return False

    row = db.query(PasteViewStat).filter(PasteViewStat.paste_hash == paste_hash).first()
    now = time.time()

    if row is None:
        db.add(PasteViewStat(paste_hash=paste_hash, view_count=1, updated_at=now))
    else:
        row.view_count += 1
        row.updated_at = now

    db.commit()

    return True
