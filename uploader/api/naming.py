"""Construct and validate the blob path for one upload.

Per §7 of ``uploader/spec/client-azure-upload.md`` a blob lives at
``container/<user-email>/<client path>``. The server owns the prefix; the
client proposes only the leaf.

That prefix is the **only** thing separating one user's files from another's
in a shared container, so this module is deliberately paranoid and
deliberately blunt. Anything it cannot prove safe is refused, including
input that is merely unusual: a rejected upload costs a user one clear error
message, whereas an escaped prefix costs everybody.

Three layers, in order:

1. The email is normalised and checked to be a plausible single path segment.
   It comes from Cloudgene, never the request body (§5.3), but is normalised
   anyway.
2. The client path is checked against a blacklist — traversal, absolute
   paths, backslashes, control characters, percent-encoding, reserved
   segments, length and segment-count ceilings.
3. The joined path is normalised and **asserted** to still begin with the
   expected prefix. Layer 3 is what catches whatever layer 2 missed; it is
   not redundant, it is the actual guarantee.

Percent signs are refused outright rather than decoded. Decoding first and
re-checking is the alternative the brief allows, but it invites a second
round of ambiguity (double encoding, overlong UTF-8) for the sake of
filenames that are already unusual in this context.
"""

import posixpath
import re

MAX_BLOB_PATH_LENGTH = 1024
MAX_PATH_SEGMENTS = 8
MAX_SEGMENT_LENGTH = 255
MAX_EMAIL_LENGTH = 254

RX_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
RX_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
RX_EMAIL = re.compile(r"^[^@\s/\\]+@[^@\s/\\]+\.[^@\s/\\]+$")

RESERVED_SEGMENTS = frozenset({"", ".", ".."})


class InvalidPathError(ValueError):
    """The client-proposed path cannot be placed under the user's prefix.

    Maps to HTTP 400 with the failing check named, so the client can fix the
    filename rather than guess.
    """


class InvalidEmailError(ValueError):
    """The email resolved from Cloudgene is not usable as a path prefix.

    This should not happen — ``cloudgene_auth`` has already rejected blank
    and non-string values — so it is a loud internal failure rather than a
    user-facing one.
    """


def normalise_email(email: str) -> str:
    """Return the lowercased, stripped email, or raise.

    The value arrives from Cloudgene's ``user.mail`` (§5.2) and is already
    lowercased there. It is normalised again here because this function is
    the one that decides where bytes land, and it should not depend on a
    caller having done the right thing.
    """
    if not isinstance(email, str):
        raise InvalidEmailError(
            f"Email is {type(email).__name__}, expected str")

    value = email.strip().lower()

    if not value:
        raise InvalidEmailError("Email is empty")

    if len(value) > MAX_EMAIL_LENGTH:
        raise InvalidEmailError("Email is implausibly long")

    if not RX_EMAIL.match(value):
        raise InvalidEmailError(
            "Email is not a usable path segment: it must contain exactly one "
            "'@', a dotted domain, and no slashes or whitespace")

    if ".." in value:
        raise InvalidEmailError("Email contains '..'")

    return value


def validate_client_path(client_path: str) -> str:
    """Return the cleaned client-proposed path, or raise.

    The returned value is always relative, forward-slash separated, and free
    of any segment that could climb out of a prefix.
    """
    if not isinstance(client_path, str):
        raise InvalidPathError(
            f"Path is {type(client_path).__name__}, expected str")

    path = client_path.strip()

    if not path:
        raise InvalidPathError("Path is empty")

    if RX_CONTROL_CHARS.search(path):
        raise InvalidPathError(
            "Path contains a control character or NUL byte")

    if "\\" in path:
        raise InvalidPathError("Path contains a backslash")

    if "%" in path:
        # Refused rather than decoded: '%2e%2e%2f' is traversal, and
        # accepting encoded input means deciding how many rounds of decoding
        # to apply before checking. See the module docstring.
        raise InvalidPathError(
            "Path contains a percent sign; URL-encoded paths are not "
            "accepted")

    if path.startswith("/"):
        raise InvalidPathError("Path is absolute (leading '/')")

    if path.endswith("/"):
        raise InvalidPathError("Path names a directory, not a file")

    if RX_WINDOWS_DRIVE.match(path):
        raise InvalidPathError("Path looks absolute (drive letter)")

    if ":" in path:
        raise InvalidPathError("Path contains a colon")

    segments = path.split("/")

    if len(segments) > MAX_PATH_SEGMENTS:
        raise InvalidPathError(
            f"Path has {len(segments)} segments; the maximum is "
            f"{MAX_PATH_SEGMENTS}")

    for segment in segments:
        if segment in RESERVED_SEGMENTS:
            raise InvalidPathError(
                f"Path contains a reserved segment: {segment!r}")

        if len(segment) > MAX_SEGMENT_LENGTH:
            raise InvalidPathError(
                f"Path segment exceeds {MAX_SEGMENT_LENGTH} characters")

        if segment != segment.strip():
            raise InvalidPathError(
                "Path segment has leading or trailing whitespace")

        if segment.endswith("."):
            # Azure and Windows both mistreat a trailing dot; the blob would
            # not be addressable by the name it was created under.
            raise InvalidPathError("Path segment ends with a dot")

    return path


def build_blob_path(email: str, client_path: str) -> str:
    """Return ``<email>/<client path>`` after proving it cannot escape.

    Args:
        email: ``user.mail`` as resolved from Cloudgene — never a value
            supplied by the client.
        client_path: the leaf path proposed by the client.

    Returns:
        The blob name relative to the container, e.g.
        ``user@example.com/run7/reads.fastq.gz``.

    Raises:
        InvalidEmailError: the prefix is unusable.
        InvalidPathError: the client path failed a check, or the joined
            path did not survive normalisation inside the prefix.
    """
    prefix = normalise_email(email) + "/"
    leaf = validate_client_path(client_path)

    joined = prefix + leaf

    if len(joined) > MAX_BLOB_PATH_LENGTH:
        raise InvalidPathError(
            f"Blob path is {len(joined)} characters; Azure's limit is "
            f"{MAX_BLOB_PATH_LENGTH}")

    # Belt and braces. If normalisation changes the path at all, some
    # component of it was not what it appeared to be, and the blacklist above
    # missed it. Refuse rather than trust the normalised form.
    normalised = posixpath.normpath(joined)

    if normalised != joined:
        raise InvalidPathError(
            "Path did not survive normalisation unchanged")

    if not normalised.startswith(prefix):
        raise InvalidPathError(
            "Path escaped the user's prefix after normalisation")

    if posixpath.isabs(normalised):
        raise InvalidPathError("Normalised path is absolute")

    return normalised


def prefix_for(email: str) -> str:
    """Return the container-relative prefix owned by one user."""
    return normalise_email(email) + "/"
