from ipaddress import ip_address
from fastapi import Request
from .settings import CLOUDFLARE


def _extract_first_header_ip(value: str | None) -> str | None:
    if not value:
        return None

    candidate = value.split(",", 1)[0].strip()
    if not candidate:
        return None

    try:
        ip_address(candidate)
    except ValueError:
        return None

    return candidate


def get_client_ip(request: Request) -> str:
    peer_ip = request.client.host if request.client else "127.0.0.1"

    if not CLOUDFLARE:
        return peer_ip

    cf_ip = _extract_first_header_ip(request.headers.get("CF-Connecting-IP"))

    return cf_ip or peer_ip
