import json
from urllib.request import Request, urlopen


MAX_LINE_BYTES = 1_048_576
MAX_BODY_BYTES = 8 * 1_048_576
MAX_RECORDS = 10_000


class StreamError(Exception):
    """The generation is incomplete or violates the expected API contract."""


class IdempotencyConflict(ValueError):
    """The operation key is already bound to a different request body."""


class InvalidSummary(ValueError):
    """The generated content does not meet the summary contract."""


def open_stream(endpoint, request_body, *, timeout=10):
    request = Request(
        endpoint,
        data=request_body,
        headers={"Content-Type": "application/json", "Accept": "application/x-ndjson"},
        method="POST",
    )
    response = urlopen(request, timeout=timeout)
    if response.status != 200 or response.headers.get_content_type() != "application/x-ndjson":
        response.close()
        raise StreamError("unexpected HTTP response")
    return response


def read_generation(response):
    fragments = []
    total_bytes = 0
    for record_number in range(1, MAX_RECORDS + 2):
        remaining = MAX_BODY_BYTES - total_bytes
        line = response.readline(min(MAX_LINE_BYTES, remaining) + 1)
        if not line:
            break
        if len(line) > remaining or record_number > MAX_RECORDS:
            raise StreamError("response budget exceeded")
        if len(line) > MAX_LINE_BYTES or not line.endswith(b"\n"):
            raise StreamError("invalid NDJSON line boundary")
        total_bytes += len(line)
        try:
            event = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise StreamError("invalid NDJSON record") from error

        if not isinstance(event, dict):
            raise StreamError("expected a JSON object")
        if "error" in event:
            raise StreamError("upstream error")
        fragment, done = event.get("response"), event.get("done")
        if not isinstance(fragment, str) or not isinstance(done, bool):
            raise StreamError("invalid generation record")
        fragments.append(fragment)

        if done:
            reason = event.get("done_reason")
            if reason == "length":
                raise StreamError("output limit")
            if reason != "stop":
                raise StreamError("unknown completion reason")
            if response.read(1):
                raise StreamError("data after completion record")
            if response.length not in (None, 0):
                raise StreamError("incomplete HTTP body")
            return "".join(fragments)

    raise StreamError("missing completion record")


def generate(endpoint, request_body):
    with open_stream(endpoint, request_body) as response:
        return read_generation(response)


def load_summary(connection, request_id, request_body):
    row = connection.execute(
        "SELECT request_body, answer FROM published WHERE request_id = ?", (request_id,)
    ).fetchone()
    if row is None:
        return None
    if row[0] != request_body:
        raise IdempotencyConflict("request ID already belongs to a different request")
    return row[1]


def save_or_get_summary(connection, request_id, request_body, candidate):
    connection.execute(
        """
        INSERT INTO published (request_id, request_body, answer)
        VALUES (?, ?, ?)
        ON CONFLICT (request_id) DO NOTHING
        """,
        (request_id, request_body, candidate),
    )
    return load_summary(connection, request_id, request_body)


def summarize(endpoint, connection, request_id, request_body):
    if connection.in_transaction:
        raise ValueError("summarize requires an idle connection")
    saved = load_summary(connection, request_id, request_body)
    if saved is not None:
        return saved

    candidate = generate(endpoint, request_body)
    if not candidate.strip():
        raise InvalidSummary("empty summary")
    with connection:
        accepted = save_or_get_summary(connection, request_id, request_body, candidate)
    return accepted
