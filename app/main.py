import time
import pathlib
from html import escape
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from sqlalchemy.orm import Session
from modules.database import PastesDatabase, get_db, init_db
from modules.edit_auth import generate_edit_token, hash_edit_token, verify_edit_token
from modules.data_structures import (
    PasteCreateMetadata, PasteDeleteMetadata, PasteEditMetadata,
    PasteEventAction, PasteEventWrite, ChallengeResponse,
    PasteCreateResponse, PasteDeleteRequest, PasteDeleteResponse,
    PasteReadResponse, PasteRequest, PasteUpdateRequest
)
from modules.network import get_client_ip
from modules.proof_of_work import generate_challenge, generate_paste_id, get_expiry_for_difficulty, verify_nonce
from modules.records import PasteRecord
from modules.rate_limit import (
    check_challenge_rate_limit, check_delete_rate_limit, check_edit_rate_limit,
    check_paste_read_rate_limit, check_rate_limit, init_limit_db,
    record_failed_token_attempt, record_paste_read_miss, record_root_miss,
)
from modules.telemetry import record_paste_event
from modules.views import record_paste_view
from modules.webhooks import send_challenge_issued_webhook, send_failure_webhook, send_paste_event_webhook


app = FastAPI(
    title="BlinkBin",
    version="1.0.0",
    description=(
        "A simple pastebin service with an open API designed to be simple and accessible for all developers. "
        "Create, retrieve, edit, and delete pastes without authentication, protected by rate limiting, "
        "proof-of-work, and temporary tokens."
    ),
    redoc_url=None  # disable redoc
)


# start DBs
init_db()
init_limit_db()


def _paste_link(request: Request, paste_hash: str) -> str:
    return f"{str(request.base_url).rstrip('/')}/{paste_hash}"


def _notify_failure(request: Request, status_code: int, detail: str) -> None:
    if status_code == 404:
        return

    send_failure_webhook(
        status_code=status_code,
        method=request.method,
        path=request.url.path,
        actor_ip=get_client_ip(request),
        detail=detail,
        user_agent=request.headers.get("user-agent"),
    )


PAGES = pathlib.Path(__file__).parent.parent / "pages"
ROOT_FILES = pathlib.Path(__file__).parent.parent / "root"


def load(file: pathlib.Path, substitutions: dict[str, str] | None = None) -> str:
    """Loads content from a file and substitutes placeholders defined in a dict"""
    with open(file, "r") as f:
        content = f.read()

    if substitutions:
        for placeholder, html in substitutions.items():
            content = content.replace(placeholder, html)

    return content


HEADER = load(PAGES / "header.html")
HEAD = load(PAGES / "head.html")
FOOTER = load(PAGES / "footer.html")
PAGES_HOME = load(
    PAGES / "index.html",
    substitutions={
        "{{header}}": HEADER,
        "{{head}}": HEAD,
        "{{footer}}": FOOTER,
    }
)


@app.exception_handler(HTTPException)
async def _http_exception_handler(request: Request, exc: HTTPException):
    _notify_failure(request, exc.status_code, str(exc.detail))
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(RequestValidationError)
async def _validation_exception_handler(request: Request, exc: RequestValidationError):
    _notify_failure(request, 422, "Request validation failed")
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    _notify_failure(request, 500, str(exc))
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse(PAGES_HOME)


@app.get("/api/challenge", response_model=ChallengeResponse, tags=["Challenge"])
def get_challenge(request: Request, db: Session = Depends(get_db)):
    ip = get_client_ip(request)
    check_challenge_rate_limit(ip, db)
    challenge, difficulty = generate_challenge(ip, db)
    expires_in = get_expiry_for_difficulty(difficulty)
    expires_at = int(time.time()) + expires_in

    send_challenge_issued_webhook(
        challenge=challenge,
        difficulty=difficulty,
        issued_ip=ip,
        expires_in=expires_in,
        expires_at=expires_at,
        method=request.method,
        path=request.url.path,
        user_agent=request.headers.get("user-agent"),
    )

    return {"challenge": challenge, "difficulty": difficulty, "expires_in": expires_in}


@app.post("/api/paste", response_model=PasteCreateResponse, tags=["Pastes"])
def post_paste(req: PasteRequest, request: Request, db: Session = Depends(get_db)):
    pastes_db = PastesDatabase(db)
    ip = get_client_ip(request)
    check_rate_limit(ip, db)

    valid, result = verify_nonce(req.challenge, req.nonce, ip, db)

    if not valid:
        raise HTTPException(status_code=400, detail=result)

    paste_hash = generate_paste_id(db)
    edit_token = generate_edit_token()
    pastes_db.add_paste(
        paste_hash=paste_hash,
        text=req.paste,
        edit_token_hash=hash_edit_token(edit_token),
        ip=ip,
    )

    paste_url = _paste_link(request, paste_hash)
    user_agent = request.headers.get("user-agent")

    record_paste_event(
        db,
        PasteEventWrite(
            action=PasteEventAction.CREATE,
            paste_hash=paste_hash,
            actor_ip=ip,
            paste_owner_ip=ip,
            request_method=request.method,
            request_path=request.url.path,
            user_agent=user_agent,
            metadata=PasteCreateMetadata(
                paste_url=paste_url,
                paste_length=len(req.paste),
                challenge=req.challenge,
                nonce=req.nonce,
            ),
        ),
    )

    send_paste_event_webhook(
        action=PasteEventAction.CREATE.value,
        paste_hash=paste_hash,
        paste_url=paste_url,
        actor_ip=ip,
        paste_owner_ip=ip,
        user_agent=user_agent,
        details={
            "Method": request.method,
            "Path": request.url.path,
            "Paste Length": str(len(req.paste)),
        },
    )

    return {"paste_hash": paste_hash, "edit_token": edit_token}


@app.get("/api/paste/{paste_hash}", response_model=PasteReadResponse, tags=["Pastes"])
def get_paste(paste_hash: str, request: Request, db: Session = Depends(get_db)):
    pastes_db = PastesDatabase(db)
    ip = get_client_ip(request)
    check_paste_read_rate_limit(ip)

    paste = pastes_db.get_paste(paste_hash)
    if not paste:
        record_paste_read_miss(ip)
        raise HTTPException(status_code=404, detail="Paste not found")

    paste_record = PasteRecord.from_model(paste)

    record_paste_view(db, paste_hash, ip)
    return paste_record.as_response()


@app.get("/api/paste/{paste_hash}/raw", response_class=PlainTextResponse, tags=["Pastes"])
def get_paste_raw(paste_hash: str, request: Request, db: Session = Depends(get_db)):
    pastes_db = PastesDatabase(db)
    ip = get_client_ip(request)
    check_paste_read_rate_limit(ip)

    paste = pastes_db.get_paste(paste_hash)
    if not paste:
        record_paste_read_miss(ip)
        raise HTTPException(status_code=404, detail="Paste not found")

    paste_record = PasteRecord.from_model(paste)

    record_paste_view(db, paste_hash, ip)
    return PlainTextResponse(paste_record.text)


@app.put("/api/paste/{paste_hash}", response_model=PasteReadResponse, tags=["Pastes"])
def edit_paste(paste_hash: str, req: PasteUpdateRequest, request: Request, db: Session = Depends(get_db)):
    pastes_db = PastesDatabase(db)
    ip = get_client_ip(request)
    paste = pastes_db.get_paste(paste_hash)
    if not paste:
        raise HTTPException(status_code=404, detail="Paste not found")

    paste_record = PasteRecord.from_model(paste)

    if not verify_edit_token(req.edit_token, paste_record.edit_token_hash):
        record_failed_token_attempt(ip)
        raise HTTPException(status_code=403, detail="Invalid edit token")

    check_edit_rate_limit(paste_record.edit_token_hash)
    paste = pastes_db.update_paste_text(paste, req.paste)

    paste_url = _paste_link(request, paste_hash)
    user_agent = request.headers.get("user-agent")

    record_paste_event(
        db,
        PasteEventWrite(
            action=PasteEventAction.EDIT,
            paste_hash=paste_hash,
            actor_ip=ip,
            paste_owner_ip=paste_record.owner_ip,
            request_method=request.method,
            request_path=request.url.path,
            user_agent=user_agent,
            metadata=PasteEditMetadata(
                paste_url=paste_url,
                paste_length=len(req.paste),
            ),
        ),
    )

    send_paste_event_webhook(
        action=PasteEventAction.EDIT.value,
        paste_hash=paste_hash,
        paste_url=paste_url,
        actor_ip=ip,
        paste_owner_ip=paste_record.owner_ip,
        user_agent=user_agent,
        details={
            "Method": request.method,
            "Path": request.url.path,
            "Paste Length": str(len(req.paste)),
        },
    )

    return PasteRecord.from_model(paste).as_response()


@app.delete("/api/paste/{paste_hash}", response_model=PasteDeleteResponse, tags=["Pastes"])
def delete_paste(paste_hash: str, req: PasteDeleteRequest, request: Request, db: Session = Depends(get_db)):
    pastes_db = PastesDatabase(db)
    ip = get_client_ip(request)
    paste = pastes_db.get_paste(paste_hash)
    if not paste:
        raise HTTPException(status_code=404, detail="Paste not found")

    paste_record = PasteRecord.from_model(paste)

    if not verify_edit_token(req.edit_token, paste_record.edit_token_hash):
        record_failed_token_attempt(ip)
        raise HTTPException(status_code=403, detail="Invalid edit token")

    check_delete_rate_limit(ip)

    owner_ip = paste_record.owner_ip
    paste_url = _paste_link(request, paste_hash)
    user_agent = request.headers.get("user-agent")
    pastes_db.delete_paste(paste)

    record_paste_event(
        db,
        PasteEventWrite(
            action=PasteEventAction.DELETE,
            paste_hash=paste_hash,
            actor_ip=ip,
            paste_owner_ip=owner_ip,
            request_method=request.method,
            request_path=request.url.path,
            user_agent=user_agent,
            metadata=PasteDeleteMetadata(paste_url=paste_url),
        ),
    )

    send_paste_event_webhook(
        action=PasteEventAction.DELETE.value,
        paste_hash=paste_hash,
        paste_url=paste_url,
        actor_ip=ip,
        paste_owner_ip=owner_ip,
        user_agent=user_agent,
        details={
            "Method": request.method,
            "Path": request.url.path,
        },
    )

    return {"deleted": True, "paste_hash": paste_hash}


# note to future self: this HAS to go at the end of this file
@app.get("/{requested_path:path}")
def root_router(requested_path: str, request: Request, db: Session = Depends(get_db)):
    pastes_db = PastesDatabase(db)
    ip = get_client_ip(request)

    if requested_path:
        file_candidate = (ROOT_FILES / requested_path).resolve()
        root_base = ROOT_FILES.resolve()
        if root_base == file_candidate or root_base in file_candidate.parents:
            if file_candidate.is_file():
                return FileResponse(file_candidate)

    paste = pastes_db.get_paste(requested_path)
    if not paste:
        record_root_miss(ip)
        return HTMLResponse((
            "<html><body style=\"background-color: black\">"
            "<h1 style=\"color: white\">404 Not Found</h1></body></html>"
        ), status_code=404)

    paste_record = PasteRecord.from_model(paste)

    record_paste_view(db, requested_path, ip)

    escaped_text = escape(paste_record.text)
    html = load(
        PAGES / "pastes.html",
        substitutions={
            "{{header}}": HEADER,
            "{{head}}": HEAD,
            "{{footer}}": FOOTER,
            "{{paste_hash}}": requested_path,
            "{{paste_text}}": escaped_text,
        },
    )
    return HTMLResponse(html)


if __name__ == "__main__":
    print("You need to run this with Uvicorn! `cd` into the `app` directory and run `uvicorn main:app`.")
