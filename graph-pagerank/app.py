
import ctypes
import gc
import json
import os
import socket
import threading
import traceback

from http.server import BaseHTTPRequestHandler, HTTPServer

import igraph


# ============================================================
# Configuration
# ============================================================

INIT_DONE = False

CONTROL_SOCKET = "/control/invoke.sock"

ENABLE_HTTP = (
    os.environ.get("PAGERANK_ENABLE_HTTP", "0") == "1"
)

# Retained graph state
GRAPH = None
GRAPH_SIZE = None
GRAPH_M = None


# ============================================================
# libc setup
#
# KSM eligibility is handled by ksm_wrap.c:
# prctl(PR_SET_MEMORY_MERGE, 1, 0, 0, 0)
#
# No Python madvise() is needed.
# ============================================================

libc = ctypes.CDLL("libc.so.6", use_errno=True)

libc.malloc_trim.argtypes = [ctypes.c_size_t]
libc.malloc_trim.restype = ctypes.c_int


# ============================================================
# Memory purging
# ============================================================

def purge_memory():

    collected = gc.collect()
    trimmed = libc.malloc_trim(0)

    return {
        "gc_collected": collected,
        "malloc_trim_result": trimmed
    }


# ============================================================
# Original Graph PageRank functionality
# ============================================================

def build_graph(size: int, m: int):

    return igraph.Graph.Barabasi(size, m)


def ensure_graph(size: int, m: int):

    global GRAPH, GRAPH_SIZE, GRAPH_M

    if (
        GRAPH is None
        or GRAPH_SIZE != size
        or GRAPH_M != m
    ):

        GRAPH = build_graph(size, m)

        GRAPH_SIZE = size
        GRAPH_M = m

    return GRAPH


def graph_ops(size: int, m: int):

    graph = ensure_graph(size, m)

    # The graph remains globally retained.
    # The returned PageRank vector is temporary.
    pagerank_values = graph.pagerank()

    first_value = float(pagerank_values[0])

    del pagerank_values

    return first_value


def graph_stats():

    return {
        "graph_initialized": GRAPH is not None,
        "graph_size": GRAPH_SIZE,
        "graph_m": GRAPH_M,
        "vertices": (
            GRAPH.vcount()
            if GRAPH is not None else 0
        ),
        "edges": (
            GRAPH.ecount()
            if GRAPH is not None else 0
        )
    }


# ============================================================
# Graph PageRank invocation
# ============================================================

def run_action(params):

    if params is None:
        params = {}

    if not isinstance(params, dict):
        raise ValueError(
            "Invocation parameters must be a JSON object"
        )

    size = int(
        params.get("size", 1000)
    )

    m = int(
        params.get("m", 10)
    )

    if size <= 0:
        raise ValueError(
            "Graph size must be positive"
        )

    if m <= 0:
        raise ValueError(
            "m must be positive"
        )

    # Build once and reuse for matching parameters.
    result = graph_ops(size, m)

    response = {
        "result": (
            f"{size} size graph PageRank finished!"
        ),
        "pagerank_first_value": result,
        "size": size,
        "m": m,
        "stats": graph_stats()
    }

    # ========================================================
    # Purge temporary memory
    #
    # Do NOT delete GRAPH.
    # It must remain resident for CRIU cloning.
    # ========================================================

    response["purge"] = purge_memory()

    return response


# ============================================================
# Request compatibility
#
# Direct:
# {"size":1000,"m":10}
#
# OpenWhisk:
# {"value":{"size":1000,"m":10}}
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

        if self.path == "/stats":

            self._send_json(
                200,
                graph_stats()
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

            request = b"".join(chunks).split(
                b"\n", 1
            )[0]

            event = json.loads(
                request.decode("utf-8")
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

    os.chmod(
        CONTROL_SOCKET,
        0o666
    )

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
            "Graph PageRank HTTP server listening on port 8080",
            flush=True
        )

        server.serve_forever()

    else:

        control_thread.join()
