import json
import sqlite3
from contextlib import closing, contextmanager
from http.client import IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from client import StreamError, generate, open_stream, save_once


FIXTURES = Path(__file__).resolve().parent / "fixtures"
REQUEST = b'{"model":"llama3.1:8b","prompt":"demo","stream":true}'


@contextmanager
def endpoint(chunks, finish_http=True, content_length=None):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            if self.path != "/api/generate" or self.rfile.read(
                int(self.headers["Content-Length"])
            ) != REQUEST:
                self.send_error(400)
                return
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
            for chunk in chunks:
                self.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
            if finish_http:
                self.wfile.write(b"0\r\n\r\n")
            else:
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


def report_replay(stop, expected_answer):
    replay = [ndjson(response="Different answer.", done=True, done_reason="stop")]
    with closing(sqlite3.connect(":memory:")) as database:
        database.execute(
            """
            CREATE TABLE published (
                request_id TEXT PRIMARY KEY NOT NULL,
                request_body BLOB NOT NULL,
                answer TEXT NOT NULL
            )
            """
        )
        database.execute("CREATE TABLE legacy (answer TEXT NOT NULL)")
        for chunks in (stop, replay):
            with endpoint(chunks) as url:
                unchecked = generate_until_eof(url)
                answer = generate(url, REQUEST)
            with database:
                database.execute("INSERT INTO legacy (answer) VALUES (?)", (unchecked,))
            with database:
                save_once(database, "request-1", REQUEST, answer)
        legacy_count = database.execute("SELECT COUNT(*) FROM legacy").fetchone()[0]
        published = database.execute("SELECT answer FROM published").fetchall()
        if published != [(expected_answer,)]:
            raise ValueError("replay changed the first stored answer")
        print(f"replayed request | {legacy_count} writes | {len(published)} write")


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

    print("case | EOF client | client with completion checks")
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
    report_replay(stop, answer)


if __name__ == "__main__":
    main()
