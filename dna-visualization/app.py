import json
import os
import tempfile
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer

from squiggle import transform

DEFAULT_FILENAME1 = "gene1.txt"
DEFAULT_FILENAME2 = "gene2.txt"
DEFAULT_LOCAL_PATH = "/action"

INIT_DONE = False


def read_text_file(path: str) -> str:
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Required input file not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def visualize(data1: str, data2: str):
    return transform(data1) + transform(data2)


def run_action(params):
    filename1 = params.get("filename1", DEFAULT_FILENAME1)
    filename2 = params.get("filename2", DEFAULT_FILENAME2)
    local_path = params.get("local_path", DEFAULT_LOCAL_PATH)
    keep_output = bool(params.get("keep_output", True))

    input_path1 = os.path.join(local_path, filename1)
    input_path2 = os.path.join(local_path, filename2)

    data1 = read_text_file(input_path1)
    data2 = read_text_file(input_path2)
    result = visualize(data1, data2)

    output_path = os.path.join(tempfile.gettempdir(), "result.txt")
    with open(output_path, "wb") as out:
        out.write(json.dumps(result).encode("utf-8"))

    output_size = os.path.getsize(output_path)

    response = {
        "result": f"Visualize {filename1}+{filename2} finished!",
        "input_1": input_path1,
        "input_2": input_path2,
        "output_path": output_path,
        "output_size_bytes": output_size,
    }

    if not keep_output and os.path.exists(output_path):
        os.remove(output_path)
        response["output_removed"] = True
    else:
        response["output_removed"] = False

    return response


class OpenWhiskHandler(BaseHTTPRequestHandler):
    def _send_json(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        if not raw.strip():
            return {}
        return json.loads(raw)

    def do_POST(self):
        global INIT_DONE
        try:
            if self.path == "/init":
                _ = self._read_json()
                INIT_DONE = True
                self._send_json(200, {"ok": True})
                return

            if self.path == "/run":
                payload = self._read_json()
                params = payload.get("value", payload)
                result = run_action(params)
                self._send_json(200, result)
                return

            self._send_json(404, {"error": "not found"})
        except Exception as exc:
            self._send_json(
                502,
                {
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                },
            )

    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {"status": "ok", "init_done": INIT_DONE})
            return
        self._send_json(404, {"error": "not found"})

    def log_message(self, format, *args):
        return


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", 8080), OpenWhiskHandler)
    print("Listening on 0.0.0.0:8080", flush=True)
    server.serve_forever()