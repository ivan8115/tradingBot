"""Database initialization — creates all tables if they don't exist."""

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from database.models import Base


def get_engine(db_path: str = "data/trading.db"):
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    return create_engine(f"sqlite:///{db_path}", echo=False)


def init_db(db_path: str = "data/trading.db"):
    """Create all tables. Safe to call on every startup (idempotent)."""
    engine = get_engine(db_path)
    Base.metadata.create_all(engine)
    return engine


def get_session_factory(db_path: str = "data/trading.db") -> sessionmaker[Session]:
    engine = init_db(db_path)
    return sessionmaker(bind=engine, autocommit=False, autoflush=False)
