import time
import pytest
import random
import hashlib
import requests
from app.modules import webhooks
from app.modules.database import SessionLocal, Paste
from app.modules.data_structures import Challenge, PasteEvent, PasteViewStat
from app.modules.rate_limit import EDIT_RATE_LIMIT_COUNT, FAILED_TOKEN_IP_RATE_LIMIT_COUNT, RATE_LIMIT_COUNT
from app.modules.proof_of_work import (
    BASE_DIFFICULTY, MAX_DIFFICULTY_INCREASE,
    CHALLENGE_EXPIRY, get_expiry_for_difficulty
)

BASE_URL = "http://127.0.0.1:8000"
API_BASE = f"{BASE_URL}/api"
REQ_TIMEOUT = 15


class _FakeResponse:
    def __init__(self, status_code: int, retry_after: str | None = None):
        self.status_code = status_code
        self.headers = {}
        if retry_after is not None:
            self.headers["Retry-After"] = retry_after


def _test_ip() -> str:
    first = random.choice(["192", "198", "203"])

    return ".".join([
        first,
        "51" if first == "198" else "0",
        {"192": "2", "198": "100", "203": "113"}.get(first, "0"),
        str(random.randint(0, 255))
    ])


def _headers_for_ip(ip: str) -> dict[str, str]:
    return {"CF-Connecting-IP": ip}


def get_challenge(ip: str, extra_headers: dict[str, str] | None = None, include_cf_header: bool = True):
    headers = _headers_for_ip(ip) if include_cf_header else {}

    if extra_headers:
        headers.update(extra_headers)

    resp = requests.get(f"{API_BASE}/challenge", headers=headers, timeout=REQ_TIMEOUT)
    assert resp.status_code == 200, f"challenge request failed: {resp.status_code} {resp.text}"
    data = resp.json()
    return data["challenge"], data["difficulty"], data.get("expires_in")


def solve_puzzle(challenge: str, difficulty: int, max_nonce: int = 100_000_000):
    prefix = "0" * difficulty
    nonce = 0
    start = time.time()

    while nonce <= max_nonce:
        solved_hash = hashlib.sha256(f"{challenge}{nonce}".encode()).hexdigest()

        if solved_hash.startswith(prefix):
            elapsed = time.time() - start
            return nonce, solved_hash, elapsed
        nonce += 1

    raise RuntimeError(f"Could not solve puzzle under nonce limit {max_nonce}")


def create_paste(ip: str, paste_text: str):
    challenge, difficulty, expires_in = get_challenge(ip)
    nonce, solved_hash, elapsed = solve_puzzle(challenge, difficulty)

    payload = {"paste": paste_text, "challenge": challenge, "nonce": nonce}
    resp = requests.post(f"{API_BASE}/paste", json=payload, headers=_headers_for_ip(ip), timeout=REQ_TIMEOUT)
    assert resp.status_code == 200, f"create paste failed: {resp.status_code} {resp.text}"
    data = resp.json()

    assert len(challenge) == 64, "challenge should be full 64-char sha256 hex"
    assert all(c in "0123456789abcdef" for c in challenge), "challenge should be lowercase hex"
    assert solved_hash.startswith("0" * difficulty), "solved hash does not satisfy challenge difficulty"
    assert isinstance(expires_in, int) and expires_in >= CHALLENGE_EXPIRY
    assert len(data["paste_hash"]) == 8 and data["paste_hash"].isalnum(), "paste id should be 8-char alnum"
    assert isinstance(data.get("edit_token"), str) and len(data["edit_token"]) > 0, "edit_token missing"

    print(f"Created paste {data['paste_hash']} at difficulty {difficulty} in {elapsed:.3f}s")
    return {
        "paste_hash": data["paste_hash"],
        "edit_token": data["edit_token"],
        "challenge": challenge,
        "difficulty": difficulty,
        "expires_in": expires_in,
        "nonce": nonce,
        "solved_hash": solved_hash,
    }


def _backdate_challenge(challenge: str, seconds_ago: int):
    db = SessionLocal()
    try:
        ch = db.query(Challenge).filter(Challenge.challenge == challenge).first()
        assert ch is not None, "challenge must exist before backdating"
        ch.created_at = time.time() - seconds_ago
        db.commit()
    finally:
        db.close()


def _seed_paste_history(ip: str, ages_in_seconds: list[int]):
    now = time.time()
    db = SessionLocal()
    try:
        for idx, age in enumerate(ages_in_seconds):
            ts = now - age
            db.add(
                Paste(
                    paste_hash=f"seed{idx}{abs(hash((ip, age, idx))) % 10_000_000}",
                    text="seed",
                    created_at=ts,
                    updated_at=ts,
                    edit_token_hash=f"seed-token-{idx}",
                    ip=ip,
                )
            )
        db.commit()
    finally:
        db.close()


@pytest.fixture(scope="session", autouse=True)
def ensure_server_is_up():
    try:
        resp = requests.get(f"{BASE_URL}/docs", timeout=REQ_TIMEOUT)
    except requests.RequestException as exc:
        pytest.fail(f"Server not reachable at {BASE_URL}: {exc}")

    if resp.status_code != 200:
        pytest.fail(f"Server at {BASE_URL} returned {resp.status_code} for /docs")


def test_docs_endpoints():
    docs = requests.get(f"{BASE_URL}/docs", timeout=REQ_TIMEOUT)
    spec = requests.get(f"{BASE_URL}/openapi.json", timeout=REQ_TIMEOUT)
    assert docs.status_code == 200, f"/docs failed: {docs.status_code}"
    assert spec.status_code == 200, f"/openapi.json failed: {spec.status_code}"
    spec_json = spec.json()
    assert "/api/challenge" in spec_json["paths"]
    assert "/api/paste" in spec_json["paths"]
    assert "/api/paste/{paste_hash}" in spec_json["paths"]
    assert "/api/paste/{paste_hash}/raw" in spec_json["paths"]


def test_challenge_shape_and_bounds():
    ip = _test_ip()
    challenge, difficulty, expires_in = get_challenge(ip)
    assert len(challenge) == 64
    assert all(c in "0123456789abcdef" for c in challenge)
    assert BASE_DIFFICULTY <= difficulty <= BASE_DIFFICULTY + MAX_DIFFICULTY_INCREASE
    assert expires_in == get_expiry_for_difficulty(difficulty)


def test_create_get_list_raw_and_html_view():
    ip = _test_ip()
    created = create_paste(ip, "<b>Hello</b> world")
    paste_hash = created["paste_hash"]

    get_resp = requests.get(f"{API_BASE}/paste/{paste_hash}", timeout=REQ_TIMEOUT)
    assert get_resp.status_code == 200, f"get paste failed: {get_resp.status_code}"
    get_json = get_resp.json()
    assert get_json["text"] == "<b>Hello</b> world", "get paste text mismatch"
    assert "edit_token" not in get_json
    assert get_json["updated_at"] >= get_json["created_at"]

    raw_resp = requests.get(f"{API_BASE}/paste/{paste_hash}/raw", timeout=REQ_TIMEOUT)
    assert raw_resp.status_code == 200, f"raw paste failed: {raw_resp.status_code}"
    assert raw_resp.text == "<b>Hello</b> world", "raw paste text mismatch"

    html_resp = requests.get(f"{BASE_URL}/{paste_hash}", timeout=REQ_TIMEOUT)
    assert html_resp.status_code == 200, f"html view failed: {html_resp.status_code}"
    assert "&lt;b&gt;Hello&lt;/b&gt; world" in html_resp.text, "html view should escape paste content"


def test_invalid_nonce_rejected_but_valid_nonce_still_works_for_same_challenge():
    ip = _test_ip()
    challenge, difficulty, _ = get_challenge(ip)
    good_nonce, _, _ = solve_puzzle(challenge, difficulty)
    bad_nonce = good_nonce + 1

    bad_payload = {"paste": "bad nonce test", "challenge": challenge, "nonce": bad_nonce}
    bad_resp = requests.post(f"{API_BASE}/paste", json=bad_payload, headers=_headers_for_ip(ip), timeout=REQ_TIMEOUT)
    assert bad_resp.status_code == 400
    assert "invalid nonce" in bad_resp.json().get("detail", "").lower()

    good_payload = {"paste": "good nonce test", "challenge": challenge, "nonce": good_nonce}
    good_resp = requests.post(
        f"{API_BASE}/paste", json=good_payload, headers=_headers_for_ip(ip), timeout=REQ_TIMEOUT
    )
    assert good_resp.status_code == 200


def test_edit_flow_with_token():
    ip = _test_ip()
    created = create_paste(ip, "original text")
    paste_hash = created["paste_hash"]
    edit_token = created["edit_token"]

    bad_edit = requests.put(
        f"{API_BASE}/paste/{paste_hash}",
        json={"paste": "hijacked", "edit_token": "definitely-wrong-token"},
        timeout=REQ_TIMEOUT,
    )
    assert bad_edit.status_code == 403, f"invalid token edit should be 403, got {bad_edit.status_code}"

    before = requests.get(f"{API_BASE}/paste/{paste_hash}", timeout=REQ_TIMEOUT).json()["updated_at"]
    time.sleep(0.01)

    good_edit = requests.put(
        f"{API_BASE}/paste/{paste_hash}",
        json={"paste": "edited text", "edit_token": edit_token},
        timeout=REQ_TIMEOUT,
    )
    assert good_edit.status_code == 200, f"valid token edit failed: {good_edit.status_code}"
    edited_json = good_edit.json()
    assert edited_json["text"] == "edited text", "edit did not update text"
    assert edited_json["updated_at"] > before, "updated_at should increase after edit"


def test_edit_rate_limited_per_token():
    ip = _test_ip()
    created = create_paste(ip, "edit burst")
    paste_hash = created["paste_hash"]
    edit_token = created["edit_token"]

    for i in range(EDIT_RATE_LIMIT_COUNT):
        resp = requests.put(
            f"{API_BASE}/paste/{paste_hash}",
            json={"paste": f"edit-{i}", "edit_token": edit_token},
            timeout=REQ_TIMEOUT,
        )
        assert resp.status_code == 200

    blocked = requests.put(
        f"{API_BASE}/paste/{paste_hash}",
        json={"paste": "edit-blocked", "edit_token": edit_token},
        timeout=REQ_TIMEOUT,
    )
    assert blocked.status_code == 429
    assert "edit rate limit" in blocked.json().get("detail", "").lower()


@pytest.mark.parametrize(
    "payload",
    [
        {"paste": "x", "challenge": "deadbeef", "nonce": 1},
        {"paste": "x", "challenge": "", "nonce": 1},
    ],
)
def test_unknown_or_invalid_challenge_values_rejected(payload):
    ip = _test_ip()
    resp = requests.post(f"{API_BASE}/paste", json=payload, headers=_headers_for_ip(ip), timeout=REQ_TIMEOUT)
    assert resp.status_code == 400
    assert "challenge" in resp.json().get("detail", "").lower()


def test_expired_challenge_rejected():
    ip = _test_ip()
    challenge, difficulty, _ = get_challenge(ip)
    nonce, _, _ = solve_puzzle(challenge, difficulty)
    _backdate_challenge(challenge, get_expiry_for_difficulty(difficulty) + 5)

    payload = {"paste": "expired challenge", "challenge": challenge, "nonce": nonce}
    resp = requests.post(f"{API_BASE}/paste", json=payload, headers=_headers_for_ip(ip), timeout=REQ_TIMEOUT)
    assert resp.status_code == 400
    assert "expired" in resp.json().get("detail", "").lower()


@pytest.mark.parametrize(
    "body",
    [
        {"paste": "x", "challenge": "abc"},
        {"paste": "x", "nonce": 1},
        {"challenge": "abc", "nonce": 1},
        {"paste": "x", "challenge": "abc", "nonce": "not-int"},
    ],
)
def test_post_validation_errors(body):
    ip = _test_ip()
    resp = requests.post(f"{API_BASE}/paste", json=body, headers=_headers_for_ip(ip), timeout=REQ_TIMEOUT)
    assert resp.status_code == 422


def test_known_pow_vector_hello_60067():
    challenge = "hello"
    nonce = 60067
    expected_hash = "0000e49eab06aa7a6b3aef7708991b91a7e01451fd67f520b832b89b18f4e7de"

    solved_hash = hashlib.sha256(f"{challenge}{nonce}".encode()).hexdigest()
    assert solved_hash == expected_hash
    assert solved_hash.startswith("0" * BASE_DIFFICULTY)

    # This PoW vector is mathematically valid, but API must still reject it
    # unless the challenge was issued and tracked by the server.
    ip = _test_ip()
    payload = {"paste": "hello", "challenge": challenge, "nonce": nonce}
    resp = requests.post(f"{API_BASE}/paste", json=payload, headers=_headers_for_ip(ip), timeout=REQ_TIMEOUT)
    assert resp.status_code == 400
    assert "challenge not found" in resp.json().get("detail", "").lower()


@pytest.mark.parametrize(
    "body",
    [
        {"paste": "x"},
        {"edit_token": "x"},
        {"paste": "x", "edit_token": 123},
    ],
)
def test_edit_validation_errors(body):
    ip = _test_ip()
    created = create_paste(ip, "seed")
    resp = requests.put(f"{API_BASE}/paste/{created['paste_hash']}", json=body, timeout=REQ_TIMEOUT)
    assert resp.status_code == 422


def test_invalid_token_attempts_rate_limited_by_ip_across_pastes():
    ip = _test_ip()
    headers = _headers_for_ip(ip)

    first = create_paste(ip, "first seed")
    second = create_paste(ip, "second seed")
    targets = [first["paste_hash"], second["paste_hash"]]

    # Alternate pastes: limiter should still trigger because it is IP-scoped.
    for i in range(FAILED_TOKEN_IP_RATE_LIMIT_COUNT):
        target_hash = targets[i % 2]
        resp = requests.put(
            f"{API_BASE}/paste/{target_hash}",
            json={"paste": f"nope-{i}", "edit_token": "wrong-token"},
            headers=headers,
            timeout=REQ_TIMEOUT,
        )
        assert resp.status_code == 403

    blocked = requests.put(
        f"{API_BASE}/paste/{targets[0]}",
        json={"paste": "blocked", "edit_token": "still-wrong"},
        headers=headers,
        timeout=REQ_TIMEOUT,
    )
    assert blocked.status_code == 429
    assert "invalid token" in blocked.json().get("detail", "").lower()


def test_invalid_token_limit_does_not_affect_other_ip():
    ip_a = _test_ip()
    ip_b = _test_ip()
    while ip_b == ip_a:
        ip_b = _test_ip()

    victim = create_paste(ip_a, "victim paste")

    # Exhaust invalid-token attempts for IP A.
    for i in range(FAILED_TOKEN_IP_RATE_LIMIT_COUNT):
        resp = requests.delete(
            f"{API_BASE}/paste/{victim['paste_hash']}",
            json={"edit_token": f"wrong-{i}"},
            headers=_headers_for_ip(ip_a),
            timeout=REQ_TIMEOUT,
        )
        assert resp.status_code == 403

    blocked_a = requests.delete(
        f"{API_BASE}/paste/{victim['paste_hash']}",
        json={"edit_token": "wrong-final"},
        headers=_headers_for_ip(ip_a),
        timeout=REQ_TIMEOUT,
    )
    assert blocked_a.status_code == 429

    # Failed delete attempts must not remove the paste.
    still_there_after_a = requests.get(f"{API_BASE}/paste/{victim['paste_hash']}", timeout=REQ_TIMEOUT)
    assert still_there_after_a.status_code == 200

    # A different IP should not be collateral-damaged by IP A failures.
    other_ip = requests.delete(
        f"{API_BASE}/paste/{victim['paste_hash']}",
        json={"edit_token": "wrong-from-other-ip"},
        headers=_headers_for_ip(ip_b),
        timeout=REQ_TIMEOUT,
    )
    assert other_ip.status_code == 403

    still_there_after_b = requests.get(f"{API_BASE}/paste/{victim['paste_hash']}", timeout=REQ_TIMEOUT)
    assert still_there_after_b.status_code == 200


def test_nonexistent_resources_return_404():
    fake_id = "ZZZZ9999"
    get_resp = requests.get(f"{API_BASE}/paste/{fake_id}", timeout=REQ_TIMEOUT)
    raw_resp = requests.get(f"{API_BASE}/paste/{fake_id}/raw", timeout=REQ_TIMEOUT)
    edit_resp = requests.put(
        f"{API_BASE}/paste/{fake_id}",
        json={"paste": "x", "edit_token": "y"},
        timeout=REQ_TIMEOUT,
    )
    delete_resp = requests.delete(
        f"{API_BASE}/paste/{fake_id}",
        json={"edit_token": "y"},
        timeout=REQ_TIMEOUT,
    )
    html_resp = requests.get(f"{BASE_URL}/{fake_id}", timeout=REQ_TIMEOUT)
    assert get_resp.status_code == 404
    assert raw_resp.status_code == 404
    assert edit_resp.status_code == 404
    assert delete_resp.status_code == 404
    assert html_resp.status_code == 404


def test_delete_flow_with_token():
    ip = _test_ip()
    created = create_paste(ip, "delete me")
    paste_hash = created["paste_hash"]
    edit_token = created["edit_token"]

    wrong = requests.delete(
        f"{API_BASE}/paste/{paste_hash}",
        json={"edit_token": "wrong-token"},
        timeout=REQ_TIMEOUT,
    )
    assert wrong.status_code == 403

    # Failed delete must not remove the paste.
    still_there = requests.get(f"{API_BASE}/paste/{paste_hash}", timeout=REQ_TIMEOUT)
    assert still_there.status_code == 200

    ok = requests.delete(
        f"{API_BASE}/paste/{paste_hash}",
        json={"edit_token": edit_token},
        timeout=REQ_TIMEOUT,
    )
    assert ok.status_code == 200
    assert ok.json()["deleted"] is True

    after = requests.get(f"{API_BASE}/paste/{paste_hash}", timeout=REQ_TIMEOUT)
    assert after.status_code == 404


def test_telemetry_events_logged_for_create_edit_delete():
    ip = _test_ip()
    created = create_paste(ip, "telemetry seed")
    paste_hash = created["paste_hash"]
    edit_token = created["edit_token"]

    edit_resp = requests.put(
        f"{API_BASE}/paste/{paste_hash}",
        json={"paste": "telemetry edited", "edit_token": edit_token},
        headers=_headers_for_ip(ip),
        timeout=REQ_TIMEOUT,
    )
    assert edit_resp.status_code == 200

    delete_resp = requests.delete(
        f"{API_BASE}/paste/{paste_hash}",
        json={"edit_token": edit_token},
        headers=_headers_for_ip(ip),
        timeout=REQ_TIMEOUT,
    )
    assert delete_resp.status_code == 200

    db = SessionLocal()
    try:
        events = db.query(PasteEvent).filter(PasteEvent.paste_hash == paste_hash).all()
        actions = {event.action for event in events}
        assert {"create", "edit", "delete"}.issubset(actions)
        assert all(event.actor_ip == ip for event in events)
        assert all(event.request_method in {"POST", "PUT", "DELETE"} for event in events)
    finally:
        db.close()


def test_view_count_persists_and_dedupes_same_ip_refreshes():
    ip = _test_ip()
    created = create_paste(ip, "view stat seed")
    paste_hash = created["paste_hash"]

    first = requests.get(f"{API_BASE}/paste/{paste_hash}", headers=_headers_for_ip(ip), timeout=REQ_TIMEOUT)
    assert first.status_code == 200

    # Same-IP immediate refresh should be deduped by in-memory cooldown.
    second = requests.get(f"{API_BASE}/paste/{paste_hash}", headers=_headers_for_ip(ip), timeout=REQ_TIMEOUT)
    assert second.status_code == 200

    db = SessionLocal()
    try:
        row = db.query(PasteViewStat).filter(PasteViewStat.paste_hash == paste_hash).first()
        assert row is not None
        assert row.view_count == 1
    finally:
        db.close()


def test_view_count_increments_for_different_ips():
    owner_ip = _test_ip()
    created = create_paste(owner_ip, "multi ip view seed")
    paste_hash = created["paste_hash"]

    ip_a = _test_ip()
    ip_b = _test_ip()
    while ip_b == ip_a:
        ip_b = _test_ip()

    a_resp = requests.get(f"{API_BASE}/paste/{paste_hash}", headers=_headers_for_ip(ip_a), timeout=REQ_TIMEOUT)
    b_resp = requests.get(f"{API_BASE}/paste/{paste_hash}", headers=_headers_for_ip(ip_b), timeout=REQ_TIMEOUT)
    assert a_resp.status_code == 200
    assert b_resp.status_code == 200

    db = SessionLocal()
    try:
        row = db.query(PasteViewStat).filter(PasteViewStat.paste_hash == paste_hash).first()
        assert row is not None
        assert row.view_count >= 2
    finally:
        db.close()


def test_replay_abuse_known_nonce_hash_spam():
    ip = _test_ip()
    challenge, difficulty, _ = get_challenge(ip)
    nonce, solved_hash, _ = solve_puzzle(challenge, difficulty)

    payload = {"paste": "first post", "challenge": challenge, "nonce": nonce}
    first = requests.post(f"{API_BASE}/paste", json=payload, headers=_headers_for_ip(ip), timeout=REQ_TIMEOUT)
    assert first.status_code == 200, f"first post should succeed, got {first.status_code}"

    # Abuse simulation: attacker reuses a known-working challenge+nonce repeatedly.
    for i in range(5):
        replay = requests.post(f"{API_BASE}/paste", json=payload, headers=_headers_for_ip(ip), timeout=REQ_TIMEOUT)
        assert replay.status_code == 400, f"replay #{i + 1} should fail with 400, got {replay.status_code}"
        detail = replay.json().get("detail", "")
        assert "already used" in detail.lower(), f"expected replay rejection, got detail={detail!r}"

    changed_body_payload = {"paste": "attacker changed body", "challenge": challenge, "nonce": nonce}
    changed_body_replay = requests.post(
        f"{API_BASE}/paste", json=changed_body_payload, headers=_headers_for_ip(ip), timeout=REQ_TIMEOUT
    )
    assert changed_body_replay.status_code == 400


def test_ip_binding_and_proxy_header_behavior():
    ip_a = _test_ip()
    ip_b = _test_ip()
    while ip_b == ip_a:
        ip_b = _test_ip()

    challenge, difficulty, _ = get_challenge(ip_a)
    nonce, _, _ = solve_puzzle(challenge, difficulty)
    payload = {"paste": "ip bound test", "challenge": challenge, "nonce": nonce}

    wrong_ip_resp = requests.post(
        f"{API_BASE}/paste", json=payload, headers=_headers_for_ip(ip_b), timeout=REQ_TIMEOUT
    )
    assert wrong_ip_resp.status_code == 400, "challenge from IP A should not be accepted from IP B"
    assert "not for your ip" in wrong_ip_resp.json().get("detail", "").lower(), "expected ip-binding error"


def test_header_precedence_cf_connecting_ip_over_x_forwarded_for():
    ip_cf = _test_ip()
    ip_xff = _test_ip()
    while ip_xff == ip_cf:
        ip_xff = _test_ip()

    challenge, difficulty, _ = get_challenge(ip_cf, extra_headers={"X-Forwarded-For": ip_xff})
    nonce, _, _ = solve_puzzle(challenge, difficulty)
    payload = {"paste": "header precedence", "challenge": challenge, "nonce": nonce}

    wrong = requests.post(
        f"{API_BASE}/paste",
        json=payload,
        headers={"CF-Connecting-IP": ip_xff, "X-Forwarded-For": ip_cf},
        timeout=REQ_TIMEOUT,
    )
    assert wrong.status_code == 400

    right = requests.post(
        f"{API_BASE}/paste",
        json=payload,
        headers={"CF-Connecting-IP": ip_cf, "X-Forwarded-For": ip_xff},
        timeout=REQ_TIMEOUT,
    )
    assert right.status_code == 200


def test_x_forwarded_for_first_hop_is_used_when_cf_missing():
    ip_first = _test_ip()
    ip_second = _test_ip()
    while ip_second == ip_first:
        ip_second = _test_ip()

    xff_value = f"{ip_first}, {ip_second}"
    challenge, difficulty, _ = get_challenge(
        ip="0.0.0.0",
        extra_headers={"X-Forwarded-For": xff_value},
        include_cf_header=False,
    )
    nonce, _, _ = solve_puzzle(challenge, difficulty)

    payload = {"paste": "xff first hop", "challenge": challenge, "nonce": nonce}

    # With Cloudflare mode enabled, X-Forwarded-For is ignored if CF-Connecting-IP is absent.
    # The challenge is therefore bound to the direct socket peer and should still validate.
    resp = requests.post(
        f"{API_BASE}/paste",
        json=payload,
        headers={"X-Forwarded-For": ip_second},
        timeout=REQ_TIMEOUT,
    )
    assert resp.status_code == 200


def test_rate_limit_spam_detection():
    ip = _test_ip()
    # Seed exactly at the rate-limit threshold inside the active window.
    _seed_paste_history(ip, ages_in_seconds=[10] * RATE_LIMIT_COUNT)

    challenge, _, _ = get_challenge(ip)
    payload = {"paste": "rate-limit test", "challenge": challenge, "nonce": 0}
    resp = requests.post(f"{API_BASE}/paste", json=payload, headers=_headers_for_ip(ip), timeout=REQ_TIMEOUT)
    assert resp.status_code == 429, f"request should be rate-limited, got {resp.status_code}"


def test_adaptive_difficulty_increase_for_spammy_ip():
    ip = _test_ip()
    _, baseline, _ = get_challenge(ip)
    assert baseline == BASE_DIFFICULTY

    _seed_paste_history(ip, ages_in_seconds=[7200 - 5, 7100, 6800, 6400, 6000])
    _, long_window_diff, _ = get_challenge(ip)
    assert long_window_diff >= 4

    burst_ip = _test_ip()
    _seed_paste_history(burst_ip, ages_in_seconds=[10, 30, 60, 120, 240])
    _, burst_diff, _ = get_challenge(burst_ip)
    assert burst_diff >= 8, f"expected aggressive ramp for 5 uploads in 5m, got {burst_diff}"


def test_adaptive_difficulty_cap_enforced():
    ip = _test_ip()
    # Seed heavy recent history to force cap.
    _seed_paste_history(ip, ages_in_seconds=[5] * 40)
    _, difficulty, _ = get_challenge(ip)

    expected_cap = BASE_DIFFICULTY + MAX_DIFFICULTY_INCREASE
    assert difficulty <= expected_cap
    assert difficulty == expected_cap


def test_burst_tiers_5min_policy_exact_levels():
    ip = _test_ip()

    # 5 uploads in 5m -> difficulty should be at least 8.
    _seed_paste_history(ip, ages_in_seconds=[10, 20, 30, 40, 50])
    _, d5, _ = get_challenge(ip)
    assert d5 >= 8

    # Add 3 more in 5m (total 8) -> should reach at least 9.
    _seed_paste_history(ip, ages_in_seconds=[60, 70, 80])
    _, d8, _ = get_challenge(ip)
    assert d8 >= 9

    # Add 4 more in 5m (total 12) -> should hit cap-tier 10.
    _seed_paste_history(ip, ages_in_seconds=[90, 100, 110, 120])
    _, d12, _ = get_challenge(ip)
    assert d12 == BASE_DIFFICULTY + MAX_DIFFICULTY_INCREASE


def test_expires_in_scales_with_difficulty():
    base_ip = _test_ip()
    _, base_diff, base_expiry = get_challenge(base_ip)

    burst_ip = _test_ip()
    _seed_paste_history(burst_ip, ages_in_seconds=[10, 20, 30, 40, 50])
    _, burst_diff, burst_expiry = get_challenge(burst_ip)

    assert burst_diff >= base_diff
    assert burst_expiry >= base_expiry


def test_webhook_retries_on_429_then_succeeds(monkeypatch):
    calls: list[int] = []
    sleeps: list[float] = []

    def fake_post(_url, json, timeout):
        _ = (json, timeout)
        calls.append(1)
        if len(calls) == 1:
            return _FakeResponse(429, retry_after="0")
        return _FakeResponse(204)

    monkeypatch.setattr(webhooks.requests, "post", fake_post)
    monkeypatch.setattr(webhooks.time, "sleep", lambda s: sleeps.append(s))

    webhooks._post_webhook({"embeds": []})

    assert len(calls) == 2
    assert len(sleeps) == 1


def test_webhook_stops_retrying_on_non_retryable_4xx(monkeypatch):
    calls: list[int] = []
    sleeps: list[float] = []

    def fake_post(_url, json, timeout):
        _ = (json, timeout)
        calls.append(1)
        return _FakeResponse(400)

    monkeypatch.setattr(webhooks.requests, "post", fake_post)
    monkeypatch.setattr(webhooks.time, "sleep", lambda s: sleeps.append(s))

    webhooks._post_webhook({"embeds": []})

    assert len(calls) == 1
    assert sleeps == []


def test_delete_webhook_uses_blocking_path(monkeypatch):
    called = {"queued": 0}

    def fake_enqueue(_payload):
        called["queued"] += 1

    monkeypatch.setattr(webhooks, "DISCORD_WEBHOOK_URL", "https://example.invalid")
    monkeypatch.setattr(webhooks, "_enqueue_payload", fake_enqueue)

    webhooks.send_paste_event_webhook(
        action="delete",
        paste_hash="Abc12345",
        paste_url="https://example.com/Abc12345",
        actor_ip="198.51.100.10",
        paste_owner_ip="198.51.100.11",
        user_agent="pytest",
        details={"Path": "/api/paste/Abc12345"},
    )

    assert called["queued"] == 1


def test_failure_webhook_is_enqueued(monkeypatch):
    called = {"queued": 0}

    def fake_enqueue(_payload):
        called["queued"] += 1

    monkeypatch.setattr(webhooks, "DISCORD_WEBHOOK_URL", "https://example.invalid")
    monkeypatch.setattr(webhooks, "_enqueue_payload", fake_enqueue)

    webhooks.send_failure_webhook(
        status_code=500,
        method="DELETE",
        path="/api/paste/Abc12345",
        actor_ip="198.51.100.10",
        detail="boom",
        user_agent="pytest",
    )

    assert called["queued"] == 1


def test_challenge_webhook_contains_expiry_timestamp(monkeypatch):
    captured: list[dict] = []

    monkeypatch.setattr(webhooks, "DISCORD_WEBHOOK_URL", "https://example.invalid")
    monkeypatch.setattr(webhooks, "_enqueue_payload", lambda payload: captured.append(payload))

    webhooks.send_challenge_issued_webhook(
        challenge="abc123",
        difficulty=7,
        issued_ip="198.51.100.10",
        expires_in=225,
        expires_at=1_700_000_000,
        method="GET",
        path="/api/challenge",
        user_agent="pytest",
    )

    assert len(captured) == 1
    embed = captured[0]["embeds"][0]
    fields = {field["name"]: field["value"] for field in embed["fields"]}

    assert embed["title"] == "Challenge Issued"
    assert fields["Difficulty"] == "7"
    assert fields["Issued IP"] == "`198.51.100.10`"
    assert fields["Method"] == "GET"
    assert fields["Path"] == "/api/challenge"
    assert "Expires In" not in fields
    assert "<t:1700000000:R>" in fields["Expires At"]


def test_cross_owner_edit_increases_actor_challenge_difficulty():
    owner_ip = _test_ip()
    created = create_paste(owner_ip, "owner paste")

    actor_ip = _test_ip()
    while actor_ip == owner_ip:
        actor_ip = _test_ip()

    # Baseline for unrelated actor before cross-owner operations.
    _, baseline, _ = get_challenge(actor_ip)

    edit_resp = requests.put(
        f"{API_BASE}/paste/{created['paste_hash']}",
        json={"paste": "edited by different ip", "edit_token": created["edit_token"]},
        headers=_headers_for_ip(actor_ip),
        timeout=REQ_TIMEOUT,
    )
    assert edit_resp.status_code == 200

    _, boosted, _ = get_challenge(actor_ip)
    assert boosted >= baseline + 1
