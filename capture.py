import argparse
import hashlib
import json
from pathlib import Path
from urllib.request import urlopen

from client import MAX_BODY_BYTES, open_stream


FIXTURES = Path(__file__).resolve().parent / "fixtures"


def capture(url, target):
    if target.exists():
        raise FileExistsError(target)
    recorded = json.loads((FIXTURES / "capture.json").read_text(encoding="utf-8"))
    model = recorded["captures"]["stop"]["request"]["model"]
    with urlopen(f"{url}/api/version", timeout=10) as response:
        version = json.load(response)["version"]
    with urlopen(f"{url}/api/tags", timeout=10) as response:
        models = json.load(response)["models"]
    digest = next(item["digest"] for item in models if item["name"] == model)

    output = {"ollama_version": version, "model_digest": digest, "captures": {}}
    results = []
    for reason, entry in recorded["captures"].items():
        request_body = json.dumps(entry["request"]).encode()
        with open_stream(f"{url}/api/generate", request_body, timeout=120) as response:
            body = response.read(MAX_BODY_BYTES + 1)
            if response.length not in (None, 0):
                raise ValueError(f"incomplete HTTP body for {reason}")
            headers = [
                f"HTTP/{response.version // 10}.{response.version % 10} "
                f"{response.status} {response.reason}"
            ]
            for name in ("Content-Type", "Date", "Transfer-Encoding", "Content-Length"):
                value = response.headers.get(name)
                if value is not None:
                    headers.append(f"{name}: {value}")
        if len(body) > MAX_BODY_BYTES or not body.endswith(b"\n"):
            raise ValueError(f"incomplete capture for {reason}")
        last = json.loads(body.splitlines()[-1])
        if last.get("done") is not True or last.get("done_reason") != reason:
            raise ValueError(f"unexpected terminal record for {reason}")

        results.append((reason, body, "\n".join(headers) + "\n"))
        output["captures"][reason] = {
            "request": entry["request"],
            "body_sha256": hashlib.sha256(body).hexdigest(),
        }

    target.mkdir()
    for reason, body, headers in results:
        (target / f"{reason}.ndjson").write_bytes(body)
        (target / f"{reason}.headers").write_bytes(headers.encode())
    (target / "capture.json").write_bytes((json.dumps(output, indent=2) + "\n").encode())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("url", help="base URL of an Ollama instance with llama3.1:8b")
    parser.add_argument("target", type=Path, help="new output directory")
    arguments = parser.parse_args()
    capture(arguments.url.rstrip("/"), arguments.target)
