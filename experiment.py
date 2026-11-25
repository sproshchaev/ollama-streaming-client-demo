import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from http.client import IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Thread
from unittest.mock import patch

from client import IdempotencyConflict, InvalidSummary, StreamError, generate, open_stream, summarize


FIXTURES = Path(__file__).resolve().parent / "fixtures"
REQUEST = b'{"model":"llama3.1:8b","prompt":"demo","stream":true}'


@contextmanager
def endpoint(chunks, finish_http=True, content_length=None, before_reply=None):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            if self.path != "/api/generate" or self.rfile.read(
                int(self.headers["Content-Length"])
            ) != REQUEST:
                self.send_error(400)
                return
            if before_reply is not None:
                before_reply()
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            if content_length is not None:
                self.send_header("Content-Length", str(content_length))
                self.end_headers()
                self.wfile.write(b"".join(chunks))
                self.close_connection = True
                return
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                for chunk in chunks:
                    self.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
                if finish_http:
                    self.wfile.write(b"0\r\n\r\n")
                else:
                    self.close_connection = True
            except (BrokenPipeError, ConnectionResetError):
                # The checked client may close the body as soon as it rejects it.
                self.close_connection = True

        def log_message(self, format, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        worker.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/api/generate"
        finally:
            server.shutdown()
            worker.join()


def generate_until_eof(url, request_body=REQUEST):
    with open_stream(url, request_body) as response:
        return "".join(json.loads(line).get("response", "") for line in response)


def ndjson(**event):
    return json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"


def captured(name, reason):
    body = (FIXTURES / name).read_bytes()
    lines = body.splitlines(keepends=True)
    events = [json.loads(line) for line in lines]
    if not lines[-1].endswith(b"\n") or events[-1].get("done_reason") != reason:
        raise ValueError(f"unexpected terminal record in {name}")
    if events[-1].get("done") is not True or not any(
        event.get("done") is False and event.get("response") for event in events[:-1]
    ):
        raise ValueError(f"missing partial text or terminal record in {name}")
    return lines


def report(name, chunks, expected_answer=None, content_length=None):
    with endpoint(chunks, content_length=content_length) as url:
        unchecked = generate_until_eof(url)
        if not unchecked:
            raise ValueError(f"legacy client did not collect any text for {name}")
        try:
            answer = generate(url, REQUEST)
        except StreamError as error:
            checked = f"rejected ({error})"
        else:
            if answer != expected_answer:
                raise ValueError(f"wrong complete answer for {name}: {answer!r}")
            checked = "accepted"
    print(f"{name} | accepted | {checked}")


def create_table(database):
    database.execute(
        """
        CREATE TABLE published (
            request_id TEXT PRIMARY KEY NOT NULL,
            request_body BLOB NOT NULL,
            answer TEXT NOT NULL
        )
        """
    )


def expect_error(error_type, message, operation, *args):
    try:
        operation(*args)
    except error_type as error:
        if str(error) != message:
            raise ValueError(f"unexpected error: {error!s}") from error
    else:
        raise ValueError(f"expected: {message}")


def report_replay(stop, expected_answer):
    offline = "unavailable://model"
    replay = [ndjson(response="Different answer.", done=True, done_reason="stop")]
    with closing(sqlite3.connect(":memory:")) as database:
        create_table(database)
        database.execute("CREATE TABLE legacy (answer TEXT NOT NULL)")
        with endpoint(stop) as original_url, endpoint(replay) as repeat_url:
            legacy_answers = [generate_until_eof(url) for url in (original_url, repeat_url)]
            with database:
                database.executemany("INSERT INTO legacy (answer) VALUES (?)",
                                     [(answer,) for answer in legacy_answers])
            summarize(original_url, database, "request-1", REQUEST)
        repeated = summarize(offline, database, "request-1", REQUEST)
        expect_error(
            IdempotencyConflict,
            "request ID already belongs to a different request",
            summarize, offline, database, "request-1", REQUEST + b" ",
        )
        legacy = database.execute("SELECT answer FROM legacy ORDER BY rowid").fetchall()
        published = database.execute("SELECT answer FROM published").fetchall()
        if legacy != [(expected_answer,), ("Different answer.",)]:
            raise ValueError("legacy client did not store both answers")
        if repeated != expected_answer or published != [(expected_answer,)]:
            raise ValueError("replay did not return and retain the first answer")
    print("replayed request | 2 writes | 1 write; original answer returned offline")
    print("same key, changed body | n/a | rejected (idempotency conflict)")


def report_competing_requests():
    second = [ndjson(response="Second answer.", done=True, done_reason="stop")]
    first = [ndjson(response="First answer.", done=True, done_reason="stop")]
    both_sent = Barrier(2, timeout=5)

    def call(url, path):
        with closing(sqlite3.connect(path)) as connection:
            return summarize(url, connection, "shared-request", REQUEST)

    with TemporaryDirectory() as temp:
        path = Path(temp) / "summaries.sqlite"
        with closing(sqlite3.connect(path)) as database:
            create_table(database)
            with (
                endpoint(first, before_reply=both_sent.wait) as first_url,
                endpoint(second, before_reply=both_sent.wait) as second_url,
                ThreadPoolExecutor(max_workers=2) as workers,
            ):
                first_result = workers.submit(call, first_url, path)
                second_result = workers.submit(call, second_url, path)
                answers = [first_result.result(), second_result.result()]
            stored = database.execute("SELECT answer FROM published").fetchall()
            if len(stored) != 1 or answers != [stored[0][0]] * 2:
                raise ValueError("competing requests returned different answers")
    print("concurrent requests | n/a | 1 write; both return the stored answer")


def report_empty_summary():
    empty = [ndjson(response="   ", done=True, done_reason="stop")]
    with closing(sqlite3.connect(":memory:")) as database, endpoint(empty) as url:
        create_table(database)
        expect_error(InvalidSummary, "empty summary", summarize, url, database, "empty", REQUEST)
        if database.execute("SELECT COUNT(*) FROM published").fetchone()[0] != 0:
            raise ValueError("empty summary was written")
    print("blank summary after stop | n/a | rejected (empty summary)")


def report_response_budget(stop):
    with endpoint(stop) as url, patch("client.MAX_BODY_BYTES", len(stop[0]) + 1):
        expect_error(StreamError, "response budget exceeded", generate, url, REQUEST)
    with endpoint(stop) as url, patch("client.MAX_RECORDS", 1):
        expect_error(StreamError, "response budget exceeded", generate, url, REQUEST)
    print("response byte or record budget exceeded | n/a | rejected (response budget exceeded)")


def report_disconnect(stop, expected_answer):
    with endpoint(stop, finish_http=False) as url:
        if generate_until_eof(url) != expected_answer:
            raise ValueError("legacy client did not collect the recorded answer")
        try:
            generate(url, REQUEST)
        except IncompleteRead:
            pass
        else:
            raise ValueError("checked client accepted an incomplete HTTP message")
    print("HTTP disconnect after done | accepted | rejected (HTTP framing)")


def main():
    stop = captured("stop.ndjson", "stop")
    length = captured("length.ndjson", "length")
    answer = "".join(json.loads(line)["response"] for line in stop)

    print("case | simple client | client with checks")
    report("recorded stop", stop, answer)
    report("EOF before done", stop[:-1])
    report("error after HTTP 200", [*stop[:-1], ndjson(error="synthetic runner failure")])
    report("recorded length", length)
    report_disconnect(stop, answer)
    report("short Content-Length body", stop, content_length=sum(map(len, stop)) + 5)

    body = ndjson(response="Да", done=False) + ndjson(
        response=".", done=True, done_reason="stop"
    )
    split = body.index("Д".encode()) + 1
    try:
        json.loads(body[:split])
    except UnicodeDecodeError:
        pass
    else:
        raise ValueError("first HTTP chunk unexpectedly decoded as UTF-8")
    report("split UTF-8; multiple lines in one chunk", [body[:split], body[split:]], "Да.")
    report_response_budget(stop)
    report_empty_summary()
    report_replay(stop, answer)
    report_competing_requests()


if __name__ == "__main__":
    main()
