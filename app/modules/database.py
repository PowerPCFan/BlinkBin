import time
from pathlib import Path
from sqlalchemy import create_engine, Float, String
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker, MappedColumn, Mapped
from typing import Generator


DATA_DIR = Path(__file__).resolve().parents[2] / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

engine = create_engine(f"sqlite:///{DATA_DIR / "pastes.db"}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False)


class Base(DeclarativeBase):
    pass


class Paste(Base):
    __tablename__ = "pastes"

    paste_hash: Mapped[str] = MappedColumn(String, primary_key=True, index=True)
    text: Mapped[str] = MappedColumn(String)
    created_at: Mapped[float] = MappedColumn(Float)
    updated_at: Mapped[float] = MappedColumn(Float)
    edit_token_hash: Mapped[str] = MappedColumn(String)
    ip: Mapped[str] = MappedColumn(String)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()

    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    Base.metadata.create_all(bind=engine)


class PastesDatabase:
    def __init__(self, session: Session):
        self.session = session

    def get_paste(self, paste_hash: str) -> "Paste | None":
        return self.session.query(Paste).filter(Paste.paste_hash == paste_hash).first()

    def add_paste(
        self,
        *,
        paste_hash: str,
        text: str,
        edit_token_hash: str,
        ip: str,
        created_at: float | None = None,
    ) -> "Paste":
        now = created_at if created_at is not None else time.time()
        paste = Paste(
            paste_hash=paste_hash,
            text=text,
            created_at=now,
            updated_at=now,
            edit_token_hash=edit_token_hash,
            ip=ip,
        )
        self.session.add(paste)
        self.session.commit()
        self.session.refresh(paste)

        return paste

    def update_paste_text(self, paste: "Paste", text: str, updated_at: float | None = None) -> "Paste":
        paste.text = text
        paste.updated_at = updated_at if updated_at is not None else time.time()
        self.session.commit()
        self.session.refresh(paste)

        return paste

    def delete_paste(self, paste: "Paste") -> None:
        self.session.delete(paste)
        self.session.commit()
