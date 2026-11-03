"""Validate collection and continuation evidence before constructing a page."""

from app.google_health.data_types import AccessMethod


class GoogleHealthError(RuntimeError):
    pass


class InvalidResponseError(GoogleHealthError):
    """A successful exchange whose body cannot authorize records or a terminal page.

    Deterministic and never retried. Messages are fixed: the rejected contents
    (keys, values, codes) stay only in the raw archive.
    """


class DuplicateResponseKeyError(InvalidResponseError):
    def __init__(self) -> None:
        super().__init__("Google Health response has duplicate object keys")


class NonStandardResponseConstantError(InvalidResponseError):
    def __init__(self) -> None:
        super().__init__("Google Health response has a non-standard JSON constant")


class ErrorResponsePayloadError(InvalidResponseError):
    """An application error under HTTP success, never inspected for retry decisions."""

    def __init__(self) -> None:
        super().__init__("Google Health response has a top-level error field")


class InvalidPaginationResponseError(InvalidResponseError):
    """An unusable continuation in a successful response, not an expired request token.

    Restarting at the first page cannot repair this response. Keep its value only in
    the raw archive; the exception is safe for logs and status summaries.
    """

    def __init__(self) -> None:
        super().__init__("Google Health response has an invalid pagination token type")


class UnstorablePaginationResponseError(InvalidResponseError):
    """A string continuation that cannot cross the hash and checkpoint boundary.

    Keep this separate from the JSON type error and expired request tokens. The
    original response belongs in the archive, never in an error or a checkpoint.
    """

    def __init__(self) -> None:
        super().__init__("Google Health response has an unstorable pagination token")


class InvalidCollectionResponseError(InvalidResponseError):
    """A different endpoint's collection cannot prove this endpoint is empty.

    Presence is decisive, including null and empty values. The wrong collection
    must never supply records, a terminal page or an expired-token diagnosis.
    """

    def __init__(self) -> None:
        super().__init__("Google Health response has an unexpected data point collection")


class InvalidCollectionItemError(InvalidResponseError):
    """A collection containing non-objects cannot supply any page evidence.

    Reject the entire page, including otherwise valid objects. Keep the item's
    value, position and type out of the exception: only the raw archive retains
    response contents. This is not an expired request token or a network failure.
    """

    def __init__(self) -> None:
        super().__init__("Google Health response has a non-object collection item")


def page_values(payload: dict, access_method: AccessMethod) -> tuple[list[dict], str | None]:
    """Keep legacy empty objects and prefer the expected field when both exist.

    Validate before creating a Page so records, checkpoints and completion proof
    cannot escape a rejected response. The caller owns the captured responses and
    must preserve them through its cancellation-safe archive on rejection.
    """
    expected, other = "dataPoints", "rollupDataPoints"
    if access_method == AccessMethod.rollup:
        expected, other = other, expected
    if expected not in payload and other in payload:
        raise InvalidCollectionResponseError()
    points = payload.get(expected, [])
    if not isinstance(points, list):
        raise GoogleHealthError("Google Health response has an invalid data point list")
    # Check the selected collection as a whole before reading its continuation.
    # Do not filter invalid items: that would let a partial or empty page prove a
    # window complete and authorize deletion markers for records still at Google.
    # Object contents keep their existing storage contract; this is no schema gate.
    if any(not isinstance(point, dict) for point in points):
        raise InvalidCollectionItemError()
    return points, _next_page_token(payload.get("nextPageToken"))


def _next_page_token(value: object) -> str | None:
    """Accept only Google's string continuation or the legacy terminal forms.

    Missing/null and the empty string end a window. Other falsey JSON values must
    fail before a page can commit records, a checkpoint or last-page evidence.
    Opaque nonempty strings are returned verbatim, including whitespace.
    """
    if value is None:
        return None
    if isinstance(value, str):
        # PostgreSQL text cannot store NUL; strict UTF-8 cannot encode surrogates.
        # JSON decoding combines valid escaped surrogate pairs into a scalar, so
        # those remain valid opaque tokens. Never trim or repair a continuation.
        # Inspect code points rather than encoding: UnicodeEncodeError retains the
        # private input in its attributes and must not enter an exception chain.
        if any(char == "\x00" or "\ud800" <= char <= "\udfff" for char in value):
            raise UnstorablePaginationResponseError()
        return value if value != "" else None
    raise InvalidPaginationResponseError()
