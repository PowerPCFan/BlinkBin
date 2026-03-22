import threading
import requests
import time
from queue import Empty, Queue
from .data_structures import PasteEventAction
from .settings import DISCORD_WEBHOOK_URL

WEBHOOK_TIMEOUT_SECONDS = 5
WEBHOOK_MAX_ATTEMPTS = 4
WEBHOOK_BASE_BACKOFF_SECONDS = 0.5
_payload_queue: Queue[dict] = Queue()
_worker_started = False
_worker_lock = threading.Lock()
_sequence_lock = threading.Lock()
_queue_sequence = 0


def _retry_after_seconds(resp: requests.Response) -> float:
    raw = resp.headers.get("Retry-After", "").strip()
    if not raw:
        return 0.0
    try:
        seconds = float(raw)
    except ValueError:
        return 0.0
    return max(0.0, seconds)


def _backoff_seconds(attempt_index: int) -> float:
    # Simple exponential backoff: 0.5, 1.0, 2.0, ...
    return WEBHOOK_BASE_BACKOFF_SECONDS * (2 ** attempt_index)


def _post_webhook(
    payload: dict,
    *,
    max_attempts: int = WEBHOOK_MAX_ATTEMPTS,
    timeout_seconds: float = WEBHOOK_TIMEOUT_SECONDS,
    sleep_between_attempts: bool = True,
) -> bool:
    for attempt in range(max_attempts):
        try:
            resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=timeout_seconds)

            if 200 <= resp.status_code < 300:
                return True

            if resp.status_code == 429:
                if sleep_between_attempts:
                    wait_seconds = _retry_after_seconds(resp) or _backoff_seconds(attempt)
                    time.sleep(wait_seconds)
                continue

            # Retry transient server-side failures.
            if 500 <= resp.status_code < 600:
                if sleep_between_attempts:
                    time.sleep(_backoff_seconds(attempt))
                continue

            # Other 4xx are treated as non-retryable.
            return False
        except requests.RequestException:
            if sleep_between_attempts:
                time.sleep(_backoff_seconds(attempt))

    # Give up after max attempts. Intentionally swallow errors.
    return False


def _webhook_worker() -> None:
    while True:
        try:
            payload = _payload_queue.get(timeout=1)
        except Empty:
            continue

        try:
            _post_webhook(payload)
        finally:
            _payload_queue.task_done()

        time.sleep(1)


def _ensure_worker_started() -> None:
    global _worker_started

    with _worker_lock:
        if _worker_started:
            return

        thread = threading.Thread(target=_webhook_worker, daemon=True)
        thread.start()
        _worker_started = True


def _enqueue_payload(payload: dict) -> None:
    _ensure_worker_started()
    _payload_queue.put(payload)


def send_paste_event_webhook(
    *,
    action: str,
    paste_hash: str,
    paste_url: str,
    actor_ip: str,
    paste_owner_ip: str | None,
    user_agent: str | None,
    details: dict[str, str],
) -> None:
    if not DISCORD_WEBHOOK_URL:
        return

    try:
        normalized_action = PasteEventAction(action)
    except ValueError:
        normalized_action = PasteEventAction.UNKNOWN

    color = {
        PasteEventAction.CREATE: 0x2ECC71,
        PasteEventAction.EDIT: 0xF1C40F,
        PasteEventAction.DELETE: 0xE74C3C,
        PasteEventAction.UNKNOWN: 0x3498DB
    }.get(normalized_action, 0x3498DB)

    action_label = {
        PasteEventAction.CREATE: "created",
        PasteEventAction.EDIT: "edited",
        PasteEventAction.DELETE: "deleted",
        PasteEventAction.UNKNOWN: "unknown"
    }.get(normalized_action, action)

    action_title = normalized_action.value.title() if normalized_action else action.title()

    fields = [
        {"name": "Paste", "value": f"[{paste_hash}]({paste_url})", "inline": False},
        {"name": "Actor IP", "value": f"`{actor_ip}`", "inline": True},
        {"name": "Owner IP", "value": f"`{paste_owner_ip}`" if paste_owner_ip else "unknown", "inline": True},
    ]

    if user_agent:
        trimmed_ua = user_agent[:200]
        fields.append({"name": "User Agent", "value": f"`{trimmed_ua}`", "inline": False})

    for key, value in details.items():
        fields.append({"name": key, "value": f"`{value}`", "inline": True})

    payload = {
        "username": "BlinkBin",
        "embeds": [
            {
                "title": f"Paste {action_title}",
                "description": f"A paste was {action_label}.",
                "color": color,
                "fields": fields,
            }
        ],
    }

    _enqueue_payload(payload)


def send_failure_webhook(
    *,
    status_code: int,
    method: str,
    path: str,
    actor_ip: str | None,
    detail: str,
    user_agent: str | None,
) -> None:
    if not DISCORD_WEBHOOK_URL:
        return

    color = 0xE67E22 if status_code < 500 else 0xC0392B
    fields = [
        {"name": "Status", "value": str(status_code), "inline": True},
        {"name": "Method", "value": method, "inline": True},
        {"name": "Path", "value": path, "inline": False},
        {"name": "Actor IP", "value": f"`{actor_ip}`" if actor_ip else "unknown", "inline": True},
        {"name": "Detail", "value": detail[:1000] if detail else "(none)", "inline": False},
    ]

    if user_agent:
        fields.append({"name": "User Agent", "value": f"`{user_agent[:200]}`", "inline": False})

    payload = {
        "username": "BlinkBin",
        "embeds": [
            {
                "title": "Request Failure",
                "description": "A non-404 API/UI request failed.",
                "color": color,
                "fields": fields,
            }
        ],
    }

    _enqueue_payload(payload)


def send_challenge_issued_webhook(
    *,
    challenge: str,
    difficulty: int,
    issued_ip: str,
    expires_in: int,
    expires_at: int,
    method: str,
    path: str,
    user_agent: str | None,
) -> None:
    if not DISCORD_WEBHOOK_URL:
        return

    fields = [
        {"name": "Challenge", "value": f"`{challenge}`", "inline": False},
        {"name": "Difficulty", "value": str(difficulty), "inline": True},
        {"name": "Issued IP", "value": f"`{issued_ip}`", "inline": True},
        {"name": "Method", "value": method, "inline": True},
        {"name": "Path", "value": path, "inline": True},
        {
            "name": "Expires At",
            "value": f"<t:{expires_at}:R> (<t:{expires_at}:F>)",
            "inline": False,
        },
    ]

    if user_agent:
        fields.append({"name": "User Agent", "value": f"`{user_agent[:200]}`", "inline": False})

    payload = {
        "username": "BlinkBin",
        "embeds": [
            {
                "title": "Challenge Issued",
                "description": "A new PoW challenge was issued.",
                "color": 0x5865F2,
                "fields": fields,
            }
        ],
    }

    _enqueue_payload(payload)
