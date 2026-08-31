import json
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer
from time import time

import numpy as np


INIT_DONE = False


def matmul(n: int, seed=None):
    """
    Builds two n x n matrices and measures only np.matmul latency.

    Note:
    - Matrix allocation and random generation are intentionally outside
      the timed section, matching your original benchmark.
    - If seed is provided, the generated matrices become deterministic.
    """

    if seed is not None:
        rng = np.random.default_rng(int(seed))
        A = rng.random((n, n), dtype=np.float64)
        B = rng.random((n, n), dtype=np.float64)
    else:
        A = np.random.rand(n, n)
        B = np.random.rand(n, n)

    start = time()
    C = np.matmul(A, B)
    latency = time() - start

    # Touch C so the computation cannot be optimized away.
    checksum = float(C[0, 0])

    return latency, checksum


def run_action(params):
    latencies = {}
    timestamps = {}

    timestamps["starting_time"] = time()

    n = int(params.get("n", 1000))
    metadata = params.get("metadata", {})

    # Optional. Useful if you want identical matrices across containers.
    seed = params.get("seed", None)

    result, checksum = matmul(n, seed=seed)

    latencies["function_execution"] = result

    timestamps["finishing_time"] = time()

    return {
        "latencies": latencies,
        "timestamps": timestamps,
        "metadata": metadata,
        "n": n,
        "seed": seed,
        "checksum": checksum,
    }


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

                # OpenWhisk usually sends {"value": {...}}
                # but this also accepts raw JSON directly.
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
            self._send_json(
                200,
                {
                    "status": "ok",
                    "init_done": INIT_DONE,
                },
            )
            return

        self._send_json(404, {"error": "not found"})

    def log_message(self, format, *args):
        return


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", 8080), OpenWhiskHandler)
    print("Listening on 0.0.0.0:8080", flush=True)
    server.serve_forever()
