"""Pending-upload records, in SQLite.

§7 of ``uploader/spec/client-azure-upload.md`` requires a record written at
issuance time: upload ID, user, blob path, declared size and type, issued-at,
expiry and state. This is that store.

Why SQLite and not a directory of JSON files
--------------------------------------------
Not durability — a JSON file per upload written with ``os.replace`` is
perfectly durable. It is that two of the requirements are **queries across
records** rather than lookups by ID:

- rate limiting, which counts one user's issuances in a rolling window;
- the expiry sweep, which finds ``state = 'pending' AND expires_at < now``.

With flat files each of those is a directory scan. ``sqlite3`` is stdlib, so
this costs no dependency and no daemon.

The two things that choice obliges
----------------------------------
``journal_mode=WAL`` and a non-zero ``busy_timeout``. Without them concurrent
writers under multiple uvicorn workers raise ``database is locked``,
intermittently, and usually first in production. Both are set on every
connection opened here; see :func:`connect`.

Everything else is deliberately narrow — a handful of functions, no ORM, no
SQL outside this module — so the store could be swapped without touching a
route handler.
"""

import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

BUSY_TIMEOUT_MS = 5000
JOURNAL_MODE = "wal"
UPLOAD_ID_BYTES = 24

STATE_PENDING = "pending"
STATE_COMPLETED = "completed"
STATE_FAILED = "failed"
STATE_EXPIRED = "expired"
TERMINAL_STATES = (STATE_COMPLETED, STATE_FAILED, STATE_EXPIRED)
VALID_STATES = (STATE_PENDING,) + TERMINAL_STATES

# Stored as ISO-8601 UTC with a fixed width, so lexicographic comparison in
# SQL is chronological comparison. A variable-width format would silently
# break the expiry sweep's `expires_at < ?`.
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"

SCHEMA = """
CREATE TABLE IF NOT EXISTS uploads (
    upload_id             TEXT PRIMARY KEY,
    user_email            TEXT NOT NULL,
    blob_path             TEXT NOT NULL,
    declared_size         INTEGER NOT NULL,
    declared_content_type TEXT NOT NULL,
    issued_at             TEXT NOT NULL,
    expires_at            TEXT NOT NULL,
    state                 TEXT NOT NULL,
    detail                TEXT,
    resolved_at           TEXT,
    renewal_count         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_uploads_owner_issued
    ON uploads (user_email, issued_at);
CREATE INDEX IF NOT EXISTS idx_uploads_state_expiry
    ON uploads (state, expires_at);
"""


class StorageError(Exception):
    """The store could not satisfy a request."""


class UploadNotFoundError(StorageError):
    """No upload with that ID belongs to that owner.

    One exception for both "does not exist" and "belongs to someone else":
    distinguishing them in a response would confirm the existence of another
    user's upload ID.
    """


class RenewalRefusedError(StorageError):
    """A renewal was refused: the record is terminal, or its cap is spent.

    Carries the record as it stood at refusal time, so the caller can tell
    the two cases apart without a second query.
    """

    def __init__(self, message: str, upload: "Upload"):
        super().__init__(message)
        self.upload = upload


@dataclass(frozen=True)
class Upload:
    """One pending-upload record."""

    upload_id: str
    user_email: str
    blob_path: str
    declared_size: int
    declared_content_type: str
    issued_at: datetime
    expires_at: datetime
    state: str
    detail: str = None
    resolved_at: datetime = None
    renewal_count: int = 0

    @property
    def is_terminal(self) -> bool:
        """True once the record can no longer change state."""
        return self.state in TERMINAL_STATES


def utcnow() -> datetime:
    """Return an aware UTC timestamp."""
    return datetime.now(timezone.utc)


def format_timestamp(value: datetime) -> str:
    """Return the fixed-width UTC string form of a timestamp."""
    return value.astimezone(timezone.utc).strftime(TIMESTAMP_FORMAT)


def parse_timestamp(value: str) -> datetime:
    """Return an aware UTC datetime from the stored string form."""
    if value is None:
        return None
    return datetime.strptime(value, TIMESTAMP_FORMAT).replace(
        tzinfo=timezone.utc)


def new_upload_id() -> str:
    """Return an unguessable upload identifier.

    Not sequential and not derived from the blob path: the ID appears in
    client-held URLs, and a guessable one would let a caller enumerate other
    users' records even though every read is owner-filtered.
    """
    return secrets.token_urlsafe(UPLOAD_ID_BYTES)


def _row_to_upload(row: sqlite3.Row) -> Upload:
    """Return an :class:`Upload` from a database row."""
    return Upload(
        upload_id=row["upload_id"],
        user_email=row["user_email"],
        blob_path=row["blob_path"],
        declared_size=row["declared_size"],
        declared_content_type=row["declared_content_type"],
        issued_at=parse_timestamp(row["issued_at"]),
        expires_at=parse_timestamp(row["expires_at"]),
        state=row["state"],
        detail=row["detail"],
        resolved_at=parse_timestamp(row["resolved_at"]),
        renewal_count=row["renewal_count"],
    )


class UploadStore:
    """A narrow, owner-scoped interface over the uploads table."""

    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)

    def connect(self) -> sqlite3.Connection:
        """Open a connection with WAL and a busy timeout already applied.

        A connection per operation rather than one shared across the app:
        SQLite connections are not safe to share between threads, and
        uvicorn runs route handlers in a thread pool.
        """
        connection = sqlite3.connect(
            self.database_path,
            timeout=BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA journal_mode={JOURNAL_MODE}")
        connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def session(self):
        """Yield a connection and close it afterwards.

        ``sqlite3.Connection`` as a context manager commits or rolls back
        but does *not* close, which under a thread pool leaks a file handle
        per request. This wrapper closes.
        """
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    def initialise(self) -> None:
        """Create the schema if it is not already present.

        Also migrates a table created before ``renewal_count`` existed:
        ``CREATE TABLE IF NOT EXISTS`` does not add columns to a table that
        is already there, so a deployment upgrading in place needs this
        migration-free default applied by hand, once.
        """
        parent = self.database_path.parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)

        with self.session() as connection:
            connection.executescript(SCHEMA)
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(uploads)")
            }
            if "renewal_count" not in columns:
                connection.execute(
                    "ALTER TABLE uploads "
                    "ADD COLUMN renewal_count INTEGER NOT NULL DEFAULT 0")

    def create(
        self,
        user_email: str,
        blob_path: str,
        declared_size: int,
        declared_content_type: str,
        expires_at: datetime,
        issued_at: datetime = None,
        upload_id: str = None,
    ) -> Upload:
        """Insert a ``pending`` record and return it."""
        record = Upload(
            upload_id=upload_id or new_upload_id(),
            user_email=user_email,
            blob_path=blob_path,
            declared_size=declared_size,
            declared_content_type=declared_content_type,
            issued_at=issued_at or utcnow(),
            expires_at=expires_at,
            state=STATE_PENDING,
        )

        with self.session() as connection:
            connection.execute(
                """
                INSERT INTO uploads (
                    upload_id, user_email, blob_path, declared_size,
                    declared_content_type, issued_at, expires_at, state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.upload_id,
                    record.user_email,
                    record.blob_path,
                    record.declared_size,
                    record.declared_content_type,
                    format_timestamp(record.issued_at),
                    format_timestamp(record.expires_at),
                    record.state,
                ),
            )

        return record

    def get(self, upload_id: str, user_email: str) -> Upload:
        """Return one upload belonging to ``user_email``.

        Owner-scoped by design: an upload ID that leaks must not become an
        authorisation bypass (§4.1 of the task brief).

        Raises:
            UploadNotFoundError: no such upload for that owner.
        """
        with self.session() as connection:
            row = connection.execute(
                "SELECT * FROM uploads WHERE upload_id = ? AND user_email = ?",
                (upload_id, user_email),
            ).fetchone()

        if row is None:
            raise UploadNotFoundError("No such upload")

        return _row_to_upload(row)

    def list_for_owner(self, user_email: str, limit: int = 100) -> list:
        """Return a user's most recent uploads, newest first."""
        with self.session() as connection:
            rows = connection.execute(
                """
                SELECT * FROM uploads
                WHERE user_email = ?
                ORDER BY issued_at DESC
                LIMIT ?
                """,
                (user_email, limit),
            ).fetchall()

        return [_row_to_upload(row) for row in rows]

    def count_issued_since(self, user_email: str, since: datetime) -> int:
        """Return how many tokens this user was issued since ``since``."""
        with self.session() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS n FROM uploads
                WHERE user_email = ? AND issued_at >= ?
                """,
                (user_email, format_timestamp(since)),
            ).fetchone()

        return row["n"]

    def oldest_issued_since(
        self,
        user_email: str,
        since: datetime,
    ) -> datetime:
        """Return the earliest issuance in the window, or ``None``.

        Used to compute ``Retry-After``: the limit relaxes when the oldest
        issuance in the rolling window falls out of it.
        """
        with self.session() as connection:
            row = connection.execute(
                """
                SELECT MIN(issued_at) AS oldest FROM uploads
                WHERE user_email = ? AND issued_at >= ?
                """,
                (user_email, format_timestamp(since)),
            ).fetchone()

        if row is None or row["oldest"] is None:
            return None

        return parse_timestamp(row["oldest"])

    def resolve(
        self,
        upload_id: str,
        state: str,
        detail: str = None,
        user_email: str = None,
    ) -> bool:
        """Move a ``pending`` record to a terminal state, exactly once.

        A conditional ``UPDATE ... WHERE state = 'pending'`` rather than
        read-then-write: the latter races, and §12 of the spec requires a
        duplicate delivery to produce a single outcome.

        Args:
            upload_id: the record to resolve.
            state: one of ``completed``, ``failed``, ``expired``.
            detail: a short, log-safe reason.
            user_email: when given, the update is additionally owner-scoped.
                Omitted by the expiry sweep, which is not acting for a user.

        Returns:
            True if this call performed the transition, False if the record
            was already terminal (or does not exist). A False return is the
            idempotent case, not an error.
        """
        if state not in TERMINAL_STATES:
            raise StorageError(
                f"{state!r} is not a terminal state; expected one of "
                f"{', '.join(TERMINAL_STATES)}")

        sql = """
            UPDATE uploads
            SET state = ?, detail = ?, resolved_at = ?
            WHERE upload_id = ? AND state = ?
        """
        params = [
            state,
            detail,
            format_timestamp(utcnow()),
            upload_id,
            STATE_PENDING,
        ]

        if user_email is not None:
            sql += " AND user_email = ?"
            params.append(user_email)

        with self.session() as connection:
            cursor = connection.execute(sql, params)
            return cursor.rowcount == 1

    def renew(
        self,
        upload_id: str,
        user_email: str,
        expires_at: datetime,
        max_renewals: int,
    ) -> Upload:
        """Extend a pending record's expiry and bump its renewal count.

        A conditional ``UPDATE`` gates both invariants at once — the record
        is still ``pending``, and its renewal count is still under the
        cap — so a record cannot be pushed past the cap by two requests
        racing each other.

        Args:
            upload_id: the record to renew.
            user_email: owner-scopes the lookup, as everywhere else.
            expires_at: the new expiry to record. The caller has already
                signed a SAS for this value; this call just makes the
                record agree with it.
            max_renewals: the per-record cap. A record already at the cap
                is refused, not silently capped.

        Raises:
            UploadNotFoundError: no such upload for that owner.
            RenewalRefusedError: the record is not ``pending``, or its
                renewal count is already at ``max_renewals``.
        """
        with self.session() as connection:
            row = connection.execute(
                "SELECT * FROM uploads WHERE upload_id = ? AND user_email = ?",
                (upload_id, user_email),
            ).fetchone()

            if row is None:
                raise UploadNotFoundError("No such upload")

            current = _row_to_upload(row)

            cursor = connection.execute(
                """
                UPDATE uploads
                SET expires_at = ?, renewal_count = renewal_count + 1
                WHERE upload_id = ? AND user_email = ? AND state = ?
                    AND renewal_count < ?
                """,
                (
                    format_timestamp(expires_at),
                    upload_id,
                    user_email,
                    STATE_PENDING,
                    max_renewals,
                ),
            )

            if cursor.rowcount != 1:
                raise RenewalRefusedError("Renewal refused", upload=current)

            row = connection.execute(
                "SELECT * FROM uploads WHERE upload_id = ?", (upload_id,),
            ).fetchone()

        return _row_to_upload(row)

    def expire_pending(self, now: datetime = None) -> int:
        """Mark every lapsed ``pending`` record ``expired``.

        A client that uploads its bytes and never calls back leaves a
        ``pending`` record behind; this is the sweep that closes it out.

        Returns:
            The number of records transitioned.
        """
        cutoff = format_timestamp(now or utcnow())

        with self.session() as connection:
            cursor = connection.execute(
                """
                UPDATE uploads
                SET state = ?, detail = ?, resolved_at = ?
                WHERE state = ? AND expires_at < ?
                """,
                (
                    STATE_EXPIRED,
                    "SAS expired before the client reported completion",
                    cutoff,
                    STATE_PENDING,
                    cutoff,
                ),
            )
            return cursor.rowcount


def window_start(window_seconds: int, now: datetime = None) -> datetime:
    """Return the start of the rolling rate-limit window."""
    return (now or utcnow()) - timedelta(seconds=window_seconds)
