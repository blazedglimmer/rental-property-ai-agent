import threading
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


_settings = get_settings()
_is_sqlite = _settings.database_url.startswith("sqlite")
engine = create_engine(
    _settings.database_url,
    connect_args={"check_same_thread": False, "timeout": 30} if _is_sqlite else {},
    pool_pre_ping=True,
)

if _is_sqlite:
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


_tls = threading.local()


def in_session() -> bool:
    return getattr(_tls, "depth", 0) > 0


def after_commit(fn) -> None:
    """Run fn after the outermost session on this thread commits (immediately if none is open).

    Side-effect writes (activity events, notifications) use this so they never open a second
    write transaction while a node's transaction holds the SQLite write lock, and are not
    recorded for work that was rolled back.
    """
    if in_session():
        _tls.pending.append(fn)
    else:
        fn()


@contextmanager
def session_scope():
    depth = getattr(_tls, "depth", 0)
    if depth == 0:
        _tls.pending = []
    _tls.depth = depth + 1
    s = SessionLocal()
    ok = False
    try:
        yield s
        s.commit()
        ok = True
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
        _tls.depth = depth
        if depth == 0:
            pending, _tls.pending = _tls.pending, []
            if ok:
                for fn in pending:
                    fn()


def init_db() -> None:
    from . import models  # noqa: F401
    Base.metadata.create_all(engine)
