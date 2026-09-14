"""Unit tests for naming.

Run from ``uploader/api``:

    python -m unittest discover -s tests -t .

The traversal corpus below is the reason this module is tested directly
rather than only through the route: the ``<user-email>/`` prefix is the
only thing separating one user's blobs from another's in a shared
container, so every input that could escape it is worth naming explicitly.
"""

import unittest

from naming import (
    InvalidEmailError,
    InvalidPathError,
    MAX_BLOB_PATH_LENGTH,
    MAX_PATH_SEGMENTS,
    build_blob_path,
    normalise_email,
    prefix_for,
    validate_client_path,
)

EMAIL = "chyde@neoformit.com"

# Every one of these must be refused. Grouped by the trick being attempted.
TRAVERSAL_CORPUS = [
    # Plain traversal
    "../other@example.com/secret.txt",
    "..",
    "../..",
    "a/../../b.txt",
    "reads/../../escape.fastq",
    "./reads.fastq",
    "a/./b.fastq",
    # Absolute
    "/etc/passwd",
    "//evil.com/x.txt",
    "/reads.fastq",
    "C:/Windows/system32.txt",
    "c:reads.fastq",
    # Windows separators
    "..\\..\\escape.txt",
    "reads\\sub\\file.fastq",
    # URL-encoded traversal
    "%2e%2e/escape.txt",
    "..%2fescape.txt",
    "%2e%2e%2fescape.txt",
    "%252e%252e/escape.txt",
    "reads%2f..%2f..%2fescape.txt",
    # Control characters and NUL
    "reads.fastq\x00.png",
    "reads\n.fastq",
    "reads\r\n.fastq",
    "reads\t.fastq",
    "\x7freads.fastq",
    # Empty segments and directory-shaped input
    "",
    "   ",
    "/",
    "reads//file.fastq",
    "reads/",
    # Segment hygiene. A space *inside* a segment is fine ("Sample (1).csv"
    # below); a segment that begins or ends with one is not, because the
    # name a client sees and the name Azure stores would differ.
    "reads /file.fastq",
    "reads/ file.fastq",
    "reads.",
    "dir./file.fastq",
    # Alternate data streams / drive-relative
    "reads.fastq:Zone.Identifier",
]

ACCEPTABLE_PATHS = [
    "reads.fastq",
    "reads.fastq.gz",
    "run7/reads_R1.fastq.gz",
    "2026-09-14/plate1/A01.ab1",
    "a-b_c.1.tsv",
    "Sample (1).csv",
    "naïve-sample.fasta",
]


class TestNormaliseEmail(unittest.TestCase):

    def test_lowercases_and_strips(self):
        self.assertEqual(
            normalise_email("  ChYde@NeoFormIT.com  "), EMAIL)

    def test_prefix_ends_with_a_slash(self):
        self.assertEqual(prefix_for("CHYDE@neoformit.com"), f"{EMAIL}/")

    def test_rejects_non_string(self):
        for value in (None, 12345, b"a@b.com", ["a@b.com"]):
            with self.subTest(value=value):
                with self.assertRaises(InvalidEmailError):
                    normalise_email(value)

    def test_rejects_empty(self):
        for value in ("", "   "):
            with self.subTest(value=value):
                with self.assertRaises(InvalidEmailError):
                    normalise_email(value)

    def test_rejects_an_email_containing_a_path_separator(self):
        # The email becomes a single path segment; a slash in it would
        # create a second one and change where the prefix boundary lies.
        for value in ("a/b@example.com", "a@ex/ample.com", "a\\b@e.com"):
            with self.subTest(value=value):
                with self.assertRaises(InvalidEmailError):
                    normalise_email(value)

    def test_rejects_traversal_shaped_emails(self):
        for value in ("..@example.com", "../../@example.com", "a@..", ".."):
            with self.subTest(value=value):
                with self.assertRaises(InvalidEmailError):
                    normalise_email(value)

    def test_rejects_something_that_is_not_an_email(self):
        for value in ("cameron", "cameron@localhost", "a@b@c.com", "@b.com"):
            with self.subTest(value=value):
                with self.assertRaises(InvalidEmailError):
                    normalise_email(value)

    def test_rejects_an_implausibly_long_email(self):
        with self.assertRaises(InvalidEmailError):
            normalise_email("a" * 300 + "@example.com")


class TestValidateClientPath(unittest.TestCase):

    def test_traversal_corpus_is_refused_entirely(self):
        for candidate in TRAVERSAL_CORPUS:
            with self.subTest(path=candidate):
                with self.assertRaises(InvalidPathError):
                    validate_client_path(candidate)

    def test_ordinary_paths_are_accepted(self):
        for candidate in ACCEPTABLE_PATHS:
            with self.subTest(path=candidate):
                self.assertEqual(validate_client_path(candidate), candidate)

    def test_rejects_non_string(self):
        for value in (None, 42, b"reads.fastq"):
            with self.subTest(value=value):
                with self.assertRaises(InvalidPathError):
                    validate_client_path(value)

    def test_rejects_too_many_segments(self):
        deep = "/".join(["d"] * (MAX_PATH_SEGMENTS + 1)) + ".fastq"
        with self.assertRaises(InvalidPathError):
            validate_client_path(deep)

    def test_accepts_the_maximum_segment_count(self):
        at_limit = "/".join(["d"] * (MAX_PATH_SEGMENTS - 1) + ["f.fastq"])
        self.assertEqual(validate_client_path(at_limit), at_limit)

    def test_rejects_an_over_long_segment(self):
        with self.assertRaises(InvalidPathError):
            validate_client_path("a" * 300 + ".fastq")


class TestBuildBlobPath(unittest.TestCase):

    def test_places_the_file_under_the_user_prefix(self):
        self.assertEqual(
            build_blob_path(EMAIL, "run7/reads.fastq.gz"),
            f"{EMAIL}/run7/reads.fastq.gz")

    def test_email_is_normalised_into_the_prefix(self):
        self.assertEqual(
            build_blob_path("  CHYDE@NeoFormIT.COM ", "reads.fastq"),
            f"{EMAIL}/reads.fastq")

    def test_every_traversal_attempt_stays_inside_the_prefix(self):
        for candidate in TRAVERSAL_CORPUS:
            with self.subTest(path=candidate):
                with self.assertRaises(InvalidPathError):
                    build_blob_path(EMAIL, candidate)

    def test_result_always_starts_with_the_prefix(self):
        for candidate in ACCEPTABLE_PATHS:
            with self.subTest(path=candidate):
                result = build_blob_path(EMAIL, candidate)
                self.assertTrue(result.startswith(f"{EMAIL}/"))

    def test_result_is_never_absolute(self):
        for candidate in ACCEPTABLE_PATHS:
            with self.subTest(path=candidate):
                self.assertFalse(build_blob_path(EMAIL, candidate)
                                 .startswith("/"))

    def test_rejects_a_path_over_the_azure_length_limit(self):
        leaf = "a" * 200
        # Eight segments of 200 characters clears 1024 without tripping the
        # per-segment or segment-count checks first.
        long_path = "/".join([leaf] * 6) + ".fastq"
        with self.assertRaises(InvalidPathError) as ctx:
            build_blob_path(EMAIL, long_path)
        self.assertIn(str(MAX_BLOB_PATH_LENGTH), str(ctx.exception))

    def test_a_bad_email_is_an_email_error_not_a_path_error(self):
        # The email comes from Cloudgene, so the two failures have different
        # audiences: one is the user's filename, the other is our problem.
        with self.assertRaises(InvalidEmailError):
            build_blob_path("../root", "reads.fastq")


if __name__ == "__main__":
    unittest.main()
