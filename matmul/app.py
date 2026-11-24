
import ctypes
import gc
import json
import os
import socket
import threading
import traceback

from http.server import BaseHTTPRequestHandler, HTTPServer
from time import time

import numpy as np


# ============================================================
# Configuration
# ============================================================

INIT_DONE = False

CONTROL_SOCKET = "/control/invoke.sock"

ENABLE_HTTP = (
    os.environ.get("MATMUL_ENABLE_HTTP", "0") == "1"
)


# ============================================================
# Memory purging
#
# KSM is enabled by ksm_wrap.c.
# No Python madvise() is required.
# ============================================================

libc = ctypes.CDLL("libc.so.6", use_errno=True)

libc.malloc_trim.argtypes = [ctypes.c_size_t]
libc.malloc_trim.restype = ctypes.c_int


def purge_memory():

    collected = gc.collect()
    trimmed = libc.malloc_trim(0)

    return {
        "gc_collected": collected,
        "malloc_trim_result": trimmed
    }


# ============================================================
# Original MatMul benchmark
# ============================================================

def matmul(n: int, seed=None):
    """
    Build two n x n matrices and measure only np.matmul.

    Matrix allocation and random generation remain
    outside the timed section.

    Matrices are not intentionally retained after
    the invocation finishes.
    """

    if seed is not None:

        rng = np.random.default_rng(int(seed))

        A = rng.random(
            (n, n),
            dtype=np.float64
        )

        B = rng.random(
            (n, n),
            dtype=np.float64
        )

    else:

        A = np.random.rand(n, n)
        B = np.random.rand(n, n)

    start = time()

    C = np.matmul(A, B)

    latency = time() - start

    # Ensure the result is accessed.
    checksum = float(C[0, 0])

    return latency, checksum


def run_action(params):

    if params is None:
        params = {}

    if not isinstance(params, dict):
        raise ValueError(
            "Invocation parameters must be a JSON object"
        )

    latencies = {}
    timestamps = {}

    timestamps["starting_time"] = time()

    n = int(params.get("n", 1000))

    if n <= 0:
        raise ValueError("n must be positive")

    metadata = params.get("metadata", {})

    seed = params.get("seed", None)

    result, checksum = matmul(n, seed=seed)

    latencies["function_execution"] = result

    timestamps["finishing_time"] = time()

    response = {
        "latencies": latencies,
        "timestamps": timestamps,
        "metadata": metadata,
        "n": n,
        "seed": seed,
        "checksum": checksum
    }

    # Matrices have gone out of scope.
    # Reclaim eligible temporary memory.
    response["purge"] = purge_memory()

    return response


# ============================================================
# Accept both direct JSON and OpenWhisk payloads
# ============================================================

def execute_event(event):

    if not isinstance(event, dict):
        raise ValueError(
            "Invocation must be a JSON object"
        )

    params = event.get("value", event)

    return run_action(params)


# ============================================================
# HTTP/OpenWhisk handler
# ============================================================

class OpenWhiskHandler(BaseHTTPRequestHandler):

    def _send_json(self, code, payload):

        body = json.dumps(payload).encode("utf-8")

        self.send_response(code)

        self.send_header(
            "Content-Type",
            "application/json"
        )

        self.send_header(
            "Content-Length",
            str(len(body))
        )

        self.end_headers()

        self.wfile.write(body)

    def _read_json(self):

        length = int(
            self.headers.get("Content-Length", "0")
        )

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

                self._read_json()

                INIT_DONE = True

                self._send_json(
                    200,
                    {"ok": True}
                )

                return

            if self.path == "/run":

                payload = self._read_json()

                result = execute_event(payload)

                self._send_json(
                    200,
                    result
                )

                return

            self._send_json(
                404,
                {"error": "not found"}
            )

        except Exception as exc:

            self._send_json(
                502,
                {
                    "error": str(exc),
                    "traceback": traceback.format_exc()
                }
            )

    def do_GET(self):

        if self.path == "/health":

            self._send_json(
                200,
                {
                    "status": "ok",
                    "init_done": INIT_DONE
                }
            )

            return

        self._send_json(
            404,
            {"error": "not found"}
        )

    def log_message(self, format, *args):
        return


# ============================================================
# Unix-domain socket invocation
# ============================================================

def handle_unix_connection(conn):

    with conn:

        try:

            chunks = []
            total_size = 0

            while True:

                data = conn.recv(65536)

                if not data:
                    break

                total_size += len(data)

                if total_size > 1024 * 1024:
                    raise ValueError(
                        "Request exceeds 1 MiB"
                    )

                chunks.append(data)

                if b"\n" in data:
                    break

            raw = b"".join(chunks).split(
                b"\n", 1
            )[0]

            event = json.loads(
                raw.decode("utf-8")
            )

            response = execute_event(event)

        except Exception as exc:

            response = {
                "error": str(exc),
                "traceback": traceback.format_exc()
            }

        try:

            conn.sendall(
                (
                    json.dumps(response) + "\n"
                ).encode("utf-8")
            )

        except OSError:
            pass


def unix_control_server():

    os.makedirs(
        os.path.dirname(CONTROL_SOCKET),
        exist_ok=True
    )

    try:
        os.unlink(CONTROL_SOCKET)
    except FileNotFoundError:
        pass

    server = socket.socket(
        socket.AF_UNIX,
        socket.SOCK_STREAM
    )

    server.bind(CONTROL_SOCKET)

    os.chmod(CONTROL_SOCKET, 0o666)

    server.listen(1)

    print(
        f"Unix control server listening on {CONTROL_SOCKET}",
        flush=True
    )

    while True:

        conn, _ = server.accept()

        handle_unix_connection(conn)


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":

    control_thread = threading.Thread(
        target=unix_control_server,
        daemon=True
    )

    control_thread.start()

    if ENABLE_HTTP:

        server = HTTPServer(
            ("0.0.0.0", 8080),
            OpenWhiskHandler
        )

        print(
            "MatMul HTTP server listening on port 8080",
            flush=True
        )

        server.serve_forever()

    else:

        control_thread.join()
