"""Unit tests for storage.

Run from ``uploader/api``:

    python -m unittest discover -s tests -t .

Each test gets its own database file in a temporary directory, so nothing
here depends on ordering or on a leftover file.
"""

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

import storage
from storage import (
    BUSY_TIMEOUT_MS,
    STATE_COMPLETED,
    STATE_EXPIRED,
    STATE_FAILED,
    STATE_PENDING,
    RenewalRefusedError,
    StorageError,
    UploadNotFoundError,
    UploadStore,
    format_timestamp,
    new_upload_id,
    parse_timestamp,
    utcnow,
)

OWNER = "chyde@neoformit.com"
OTHER_OWNER = "someone.else@example.com"
CONTENT_TYPE = "application/gzip"


class StoreTestCase(unittest.TestCase):
    """Base class providing an initialised store on a temporary file."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = UploadStore(Path(self.tmp.name) / "uploads.sqlite3")
        self.store.initialise()

    def make_upload(
        self,
        owner: str = OWNER,
        blob_path: str = None,
        size: int = 1024,
        issued_at=None,
        expires_in_seconds: int = 86400,
    ):
        """Insert one pending record and return it."""
        now = issued_at or utcnow()
        return self.store.create(
            user_email=owner,
            blob_path=blob_path or f"{owner}/reads.fastq.gz",
            declared_size=size,
            declared_content_type=CONTENT_TYPE,
            issued_at=now,
            expires_at=now + timedelta(seconds=expires_in_seconds),
        )


class TestConnectionPragmas(StoreTestCase):
    """The one real cost of choosing SQLite, per §4.4 of the task brief."""

    def test_journal_mode_is_wal(self):
        with self.store.session() as connection:
            mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode.lower(), "wal")

    def test_busy_timeout_is_non_zero(self):
        with self.store.session() as connection:
            timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
        self.assertGreater(timeout, 0)
        self.assertEqual(timeout, BUSY_TIMEOUT_MS)

    def test_every_connection_gets_the_pragmas_not_just_the_first(self):
        # Connections are opened per operation, so the pragmas must be
        # applied in connect(), not once at initialise().
        for _ in range(3):
            with self.store.session() as connection:
                self.assertEqual(
                    connection.execute(
                        "PRAGMA journal_mode").fetchone()[0].lower(),
                    "wal")
                self.assertEqual(
                    connection.execute("PRAGMA busy_timeout").fetchone()[0],
                    BUSY_TIMEOUT_MS)

    def test_initialise_is_idempotent(self):
        self.store.initialise()
        self.store.initialise()
        self.assertEqual(self.store.list_for_owner(OWNER), [])


class TestUploadIds(unittest.TestCase):

    def test_ids_are_unguessable_and_unique(self):
        ids = {new_upload_id() for _ in range(1000)}
        self.assertEqual(len(ids), 1000)
        for value in list(ids)[:20]:
            self.assertGreaterEqual(len(value), 32)

    def test_ids_are_not_sequential(self):
        first, second = new_upload_id(), new_upload_id()
        self.assertNotEqual(first, second)


class TestTimestamps(unittest.TestCase):

    def test_round_trip(self):
        now = utcnow()
        self.assertEqual(parse_timestamp(format_timestamp(now)), now)

    def test_lexicographic_order_is_chronological(self):
        # The expiry sweep and the rate-limit window both compare these as
        # strings in SQL. A variable-width format would break both silently.
        now = utcnow()
        earlier = format_timestamp(now - timedelta(days=400))
        later = format_timestamp(now)
        self.assertLess(earlier, later)

    def test_parses_none(self):
        self.assertIsNone(parse_timestamp(None))


class TestCreateAndGet(StoreTestCase):

    def test_created_record_is_pending(self):
        record = self.make_upload()
        self.assertEqual(record.state, STATE_PENDING)
        self.assertFalse(record.is_terminal)

    def test_record_round_trips(self):
        record = self.make_upload()
        fetched = self.store.get(record.upload_id, OWNER)
        self.assertEqual(fetched.upload_id, record.upload_id)
        self.assertEqual(fetched.blob_path, record.blob_path)
        self.assertEqual(fetched.declared_size, record.declared_size)
        self.assertEqual(fetched.declared_content_type, CONTENT_TYPE)
        self.assertEqual(fetched.issued_at, record.issued_at)
        self.assertEqual(fetched.expires_at, record.expires_at)

    def test_unknown_id_raises(self):
        with self.assertRaises(UploadNotFoundError):
            self.store.get("no-such-id", OWNER)


class TestOwnerScoping(StoreTestCase):
    """An upload ID that leaks must not become an authorisation bypass."""

    def test_user_b_cannot_read_user_a_upload_by_id(self):
        record = self.make_upload(owner=OWNER)
        with self.assertRaises(UploadNotFoundError):
            self.store.get(record.upload_id, OTHER_OWNER)

    def test_listing_is_owner_scoped(self):
        self.make_upload(owner=OWNER)
        self.make_upload(owner=OTHER_OWNER)
        mine = self.store.list_for_owner(OWNER)
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0].user_email, OWNER)

    def test_user_b_cannot_resolve_user_a_upload(self):
        record = self.make_upload(owner=OWNER)
        self.assertFalse(
            self.store.resolve(
                record.upload_id, STATE_FAILED, user_email=OTHER_OWNER))
        self.assertEqual(
            self.store.get(record.upload_id, OWNER).state, STATE_PENDING)


class TestStateTransitions(StoreTestCase):

    def test_pending_to_completed(self):
        record = self.make_upload()
        self.assertTrue(
            self.store.resolve(record.upload_id, STATE_COMPLETED))
        fetched = self.store.get(record.upload_id, OWNER)
        self.assertEqual(fetched.state, STATE_COMPLETED)
        self.assertIsNotNone(fetched.resolved_at)
        self.assertTrue(fetched.is_terminal)

    def test_resolution_is_idempotent(self):
        # §12 of the spec: a duplicate delivery produces a single outcome.
        record = self.make_upload()
        self.assertTrue(
            self.store.resolve(record.upload_id, STATE_COMPLETED))
        self.assertFalse(
            self.store.resolve(record.upload_id, STATE_COMPLETED))
        self.assertEqual(
            self.store.get(record.upload_id, OWNER).state, STATE_COMPLETED)

    def test_a_terminal_record_cannot_be_re_resolved_to_another_state(self):
        record = self.make_upload()
        self.store.resolve(record.upload_id, STATE_COMPLETED)
        self.assertFalse(self.store.resolve(record.upload_id, STATE_FAILED))
        self.assertEqual(
            self.store.get(record.upload_id, OWNER).state, STATE_COMPLETED)

    def test_detail_is_stored(self):
        record = self.make_upload()
        self.store.resolve(
            record.upload_id, STATE_FAILED, detail="No blob at that path")
        self.assertEqual(
            self.store.get(record.upload_id, OWNER).detail,
            "No blob at that path")

    def test_resolving_an_unknown_id_is_false_not_an_error(self):
        self.assertFalse(self.store.resolve("nope", STATE_FAILED))

    def test_pending_is_not_a_terminal_state(self):
        record = self.make_upload()
        with self.assertRaises(StorageError):
            self.store.resolve(record.upload_id, STATE_PENDING)

    def test_an_invented_state_is_refused(self):
        record = self.make_upload()
        with self.assertRaises(StorageError):
            self.store.resolve(record.upload_id, "promoted")


class TestExpirySweep(StoreTestCase):

    def test_lapsed_pending_records_are_expired(self):
        stale = self.make_upload(
            issued_at=utcnow() - timedelta(days=2), expires_in_seconds=60)
        fresh = self.make_upload()

        self.assertEqual(self.store.expire_pending(), 1)
        self.assertEqual(
            self.store.get(stale.upload_id, OWNER).state, STATE_EXPIRED)
        self.assertEqual(
            self.store.get(fresh.upload_id, OWNER).state, STATE_PENDING)

    def test_terminal_records_are_left_alone(self):
        record = self.make_upload(
            issued_at=utcnow() - timedelta(days=2), expires_in_seconds=60)
        self.store.resolve(record.upload_id, STATE_COMPLETED)

        self.assertEqual(self.store.expire_pending(), 0)
        self.assertEqual(
            self.store.get(record.upload_id, OWNER).state, STATE_COMPLETED)

    def test_sweep_is_idempotent(self):
        self.make_upload(
            issued_at=utcnow() - timedelta(days=2), expires_in_seconds=60)
        self.assertEqual(self.store.expire_pending(), 1)
        self.assertEqual(self.store.expire_pending(), 0)


class TestRateLimitQueries(StoreTestCase):

    def test_counts_only_this_user(self):
        for _ in range(3):
            self.make_upload(owner=OWNER)
        self.make_upload(owner=OTHER_OWNER)

        since = storage.window_start(3600)
        self.assertEqual(self.store.count_issued_since(OWNER, since), 3)
        self.assertEqual(
            self.store.count_issued_since(OTHER_OWNER, since), 1)

    def test_counts_only_within_the_window(self):
        self.make_upload(issued_at=utcnow() - timedelta(hours=5))
        self.make_upload()

        since = storage.window_start(3600)
        self.assertEqual(self.store.count_issued_since(OWNER, since), 1)

    def test_oldest_in_window_drives_retry_after(self):
        oldest_at = utcnow() - timedelta(minutes=30)
        self.make_upload(issued_at=oldest_at)
        self.make_upload()

        since = storage.window_start(3600)
        oldest = self.store.oldest_issued_since(OWNER, since)
        self.assertEqual(oldest, oldest_at)

    def test_oldest_is_none_when_the_window_is_empty(self):
        since = storage.window_start(3600)
        self.assertIsNone(self.store.oldest_issued_since(OWNER, since))

    def test_expired_records_still_count_against_the_budget(self):
        # The budget is on *issuance*, not on success — otherwise a client
        # that never completes gets unlimited tokens.
        record = self.make_upload()
        self.store.resolve(record.upload_id, STATE_FAILED)
        since = storage.window_start(3600)
        self.assertEqual(self.store.count_issued_since(OWNER, since), 1)


class TestRenewalCountMigration(unittest.TestCase):
    """A deployment upgrading in place has a table with no such column."""

    def test_initialise_adds_the_column_to_a_pre_existing_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "uploads.sqlite3"
            store = UploadStore(db_path)
            store.initialise()

            # Simulate the table as it existed before this task: drop the
            # column back off.
            with store.session() as connection:
                connection.executescript("""
                    CREATE TABLE uploads_old AS SELECT
                        upload_id, user_email, blob_path, declared_size,
                        declared_content_type, issued_at, expires_at,
                        state, detail, resolved_at
                    FROM uploads;
                    DROP TABLE uploads;
                    ALTER TABLE uploads_old RENAME TO uploads;
                """)
                columns = {
                    row["name"] for row in
                    connection.execute("PRAGMA table_info(uploads)")
                }
                self.assertNotIn("renewal_count", columns)

            store.initialise()

            with store.session() as connection:
                columns = {
                    row["name"] for row in
                    connection.execute("PRAGMA table_info(uploads)")
                }
            self.assertIn("renewal_count", columns)


class TestRenewal(StoreTestCase):

    def test_renewal_extends_expiry(self):
        record = self.make_upload(expires_in_seconds=60)
        new_expiry = utcnow() + timedelta(hours=24)

        renewed = self.store.renew(
            record.upload_id, OWNER, new_expiry, max_renewals=10)

        self.assertEqual(renewed.expires_at, new_expiry)
        self.assertGreater(renewed.expires_at, record.expires_at)

    def test_renewal_bumps_the_count(self):
        record = self.make_upload()
        self.store.renew(
            record.upload_id, OWNER, utcnow() + timedelta(hours=24),
            max_renewals=10)
        self.assertEqual(
            self.store.get(record.upload_id, OWNER).renewal_count, 1)

    def test_renewed_record_is_still_the_same_blob(self):
        record = self.make_upload()
        renewed = self.store.renew(
            record.upload_id, OWNER, utcnow() + timedelta(hours=24),
            max_renewals=10)
        self.assertEqual(renewed.blob_path, record.blob_path)

    def test_a_renewed_record_survives_a_sweep_that_would_have_expired_it(
            self):
        record = self.make_upload(expires_in_seconds=60)
        self.store.renew(
            record.upload_id, OWNER, utcnow() + timedelta(hours=24),
            max_renewals=10)

        self.assertEqual(self.store.expire_pending(), 0)
        self.assertEqual(
            self.store.get(record.upload_id, OWNER).state, STATE_PENDING)

    def test_renewal_of_unknown_id_is_not_found(self):
        with self.assertRaises(UploadNotFoundError):
            self.store.renew(
                "no-such-id", OWNER, utcnow() + timedelta(hours=24),
                max_renewals=10)

    def test_renewal_of_another_owners_upload_is_not_found(self):
        record = self.make_upload(owner=OWNER)
        with self.assertRaises(UploadNotFoundError):
            self.store.renew(
                record.upload_id, OTHER_OWNER,
                utcnow() + timedelta(hours=24), max_renewals=10)

    def test_renewal_of_a_completed_record_is_refused(self):
        record = self.make_upload()
        self.store.resolve(record.upload_id, STATE_COMPLETED)
        with self.assertRaises(RenewalRefusedError):
            self.store.renew(
                record.upload_id, OWNER, utcnow() + timedelta(hours=24),
                max_renewals=10)

    def test_renewal_of_a_failed_record_is_refused(self):
        record = self.make_upload()
        self.store.resolve(record.upload_id, STATE_FAILED)
        with self.assertRaises(RenewalRefusedError):
            self.store.renew(
                record.upload_id, OWNER, utcnow() + timedelta(hours=24),
                max_renewals=10)

    def test_renewal_of_an_expired_record_is_refused(self):
        record = self.make_upload()
        self.store.resolve(record.upload_id, STATE_EXPIRED)
        with self.assertRaises(RenewalRefusedError):
            self.store.renew(
                record.upload_id, OWNER, utcnow() + timedelta(hours=24),
                max_renewals=10)

    def test_a_refusal_carries_the_current_record(self):
        record = self.make_upload()
        self.store.resolve(record.upload_id, STATE_COMPLETED)
        with self.assertRaises(RenewalRefusedError) as ctx:
            self.store.renew(
                record.upload_id, OWNER, utcnow() + timedelta(hours=24),
                max_renewals=10)
        self.assertEqual(ctx.exception.upload.state, STATE_COMPLETED)

    def test_the_renewal_cap_is_enforced(self):
        record = self.make_upload()
        for _ in range(3):
            record = self.store.renew(
                record.upload_id, OWNER, utcnow() + timedelta(hours=24),
                max_renewals=3)

        with self.assertRaises(RenewalRefusedError) as ctx:
            self.store.renew(
                record.upload_id, OWNER, utcnow() + timedelta(hours=24),
                max_renewals=3)
        self.assertEqual(ctx.exception.upload.renewal_count, 3)
        self.assertEqual(ctx.exception.upload.state, STATE_PENDING)

    def test_a_capped_record_is_not_mutated_by_the_refused_attempt(self):
        record = self.make_upload()
        for _ in range(3):
            record = self.store.renew(
                record.upload_id, OWNER, utcnow() + timedelta(hours=24),
                max_renewals=3)
        stale_expiry = record.expires_at

        with self.assertRaises(RenewalRefusedError):
            self.store.renew(
                record.upload_id, OWNER, utcnow() + timedelta(hours=48),
                max_renewals=3)

        self.assertEqual(
            self.store.get(record.upload_id, OWNER).expires_at,
            stale_expiry)


if __name__ == "__main__":
    unittest.main()
