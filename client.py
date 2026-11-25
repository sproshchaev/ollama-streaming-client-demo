import json
from urllib.request import Request, urlopen


MAX_LINE_BYTES = 1_048_576


class StreamError(Exception):
    """The generation is incomplete or violates the expected API contract."""


def open_stream(endpoint, request_body):
    request = Request(
        endpoint,
        data=request_body,
        headers={"Content-Type": "application/json", "Accept": "application/x-ndjson"},
        method="POST",
    )
    response = urlopen(request, timeout=10)
    if response.status != 200 or response.headers.get_content_type() != "application/x-ndjson":
        response.close()
        raise StreamError("unexpected HTTP response")
    return response


def read_generation(response):
    fragments = []
    while line := response.readline(MAX_LINE_BYTES + 1):
        if len(line) > MAX_LINE_BYTES or not line.endswith(b"\n"):
            raise StreamError("invalid NDJSON line boundary")
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


def save_once(connection, request_id, request_body, answer):
    cursor = connection.execute(
        """
        INSERT INTO published (request_id, request_body, answer)
        VALUES (?, ?, ?)
        ON CONFLICT (request_id) DO NOTHING
        """,
        (request_id, request_body, answer),
    )
    if cursor.rowcount == 1:
        return True
    stored_request = connection.execute(
        "SELECT request_body FROM published WHERE request_id = ?", (request_id,)
    ).fetchone()[0]
    if stored_request != request_body:
        raise ValueError("request ID already belongs to a different request")
    return False
