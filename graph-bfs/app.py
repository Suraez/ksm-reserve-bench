
import ctypes
import gc
import json
import os
import socket
import threading
import traceback

from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np


# ============================================================
# Configuration
# ============================================================

INIT_DONE = False

CONTROL_SOCKET = "/control/invoke.sock"

ENABLE_HTTP = (
    os.environ.get("BFS_ENABLE_HTTP", "0") == "1"
)

# Global retained graph.
# These arrays MUST remain alive across warm invocations.
INDPTR = None
INDICES = None
GRAPH_NODES = None
GRAPH_DEGREE = None


# ============================================================
# libc configuration
#
# KSM is enabled by ksm_wrap.c:
# prctl(PR_SET_MEMORY_MERGE, 1, 0, 0, 0)
#
# No Python madvise(MADV_MERGEABLE) is necessary.
# ============================================================

libc = ctypes.CDLL("libc.so.6", use_errno=True)

libc.malloc_trim.argtypes = [ctypes.c_size_t]
libc.malloc_trim.restype = ctypes.c_int


# ============================================================
# Post-invocation memory purging
# ============================================================

def purge_memory():

    collected = gc.collect()
    trimmed = libc.malloc_trim(0)

    return {
        "gc_collected": collected,
        "malloc_trim_result": trimmed
    }


# ============================================================
# Original deterministic CSR graph builder
# ============================================================

def build_deterministic_csr_graph(num_nodes: int, degree: int):

    global INDPTR, INDICES, GRAPH_NODES, GRAPH_DEGREE

    if (
        INDPTR is not None
        and GRAPH_NODES == num_nodes
        and GRAPH_DEGREE == degree
    ):
        return

    total_edges = num_nodes * degree

    indptr = np.zeros(
        num_nodes + 1,
        dtype=np.int64
    )

    indices = np.zeros(
        total_edges,
        dtype=np.int32
    )

    edge_pos = 0

    for i in range(num_nodes):

        indptr[i] = edge_pos

        for j in range(1, degree + 1):

            indices[edge_pos] = (
                i + j
            ) % num_nodes

            edge_pos += 1

    indptr[num_nodes] = edge_pos

    INDPTR = indptr
    INDICES = indices
    GRAPH_NODES = num_nodes
    GRAPH_DEGREE = degree


# ============================================================
# Original BFS
# ============================================================

def bfs_csr(start_node: int = 0):

    global INDPTR, INDICES, GRAPH_NODES

    visited = np.zeros(
        GRAPH_NODES,
        dtype=np.uint8
    )

    q = deque([start_node])

    visited[start_node] = 1

    count = 1

    while q:

        u = q.popleft()

        begin = INDPTR[u]
        end = INDPTR[u + 1]

        for idx in range(begin, end):

            v = int(INDICES[idx])

            if not visited[v]:

                visited[v] = 1

                q.append(v)

                count += 1

    return count


# ============================================================
# Retained graph statistics
# ============================================================

def graph_stats():

    total_bytes = 0

    if INDPTR is not None:
        total_bytes += INDPTR.nbytes

    if INDICES is not None:
        total_bytes += INDICES.nbytes

    return {
        "nodes": GRAPH_NODES,
        "degree": GRAPH_DEGREE,
        "bytes_retained": int(total_bytes),
        "mb_retained": round(
            total_bytes / (1024 * 1024),
            2
        ),
        "indptr_bytes": (
            int(INDPTR.nbytes)
            if INDPTR is not None else 0
        ),
        "indices_bytes": (
            int(INDICES.nbytes)
            if INDICES is not None else 0
        )
    }


# ============================================================
# Graph BFS invocation
# ============================================================

def run_action(params):

    if params is None:
        params = {}

    if not isinstance(params, dict):
        raise ValueError(
            "Invocation parameters must be a JSON object"
        )

    num_nodes = int(
        params.get("num_nodes", 5_000_000)
    )

    degree = int(
        params.get("degree", 10)
    )

    start_node = int(
        params.get("start_node", 0)
    )

    if num_nodes <= 0 or num_nodes > np.iinfo(np.int32).max:
        raise ValueError("Invalid num_nodes")

    if degree < 0:
        raise ValueError("degree cannot be negative")

    if not 0 <= start_node < num_nodes:
        raise ValueError("start_node is out of range")

    # Retain graph when the configuration matches.
    build_deterministic_csr_graph(
        num_nodes,
        degree
    )

    # Execute BFS.
    visited_count = bfs_csr(start_node)

    response = {
        "message": (
            f"BFS finished for graph with "
            f"{num_nodes} nodes, degree {degree}"
        ),
        "visited_count": visited_count,
        "start_node": start_node,
        "stats": graph_stats()
    }

    # ========================================================
    # Purge temporary memory.
    #
    # Do NOT delete INDPTR or INDICES.
    # They must remain resident for cloning.
    # ========================================================

    response["purge"] = purge_memory()

    return response


# ============================================================
# Direct and OpenWhisk-style input
# ============================================================

def execute_event(event):

    if not isinstance(event, dict):
        raise ValueError(
            "Invocation must be a JSON object"
        )

    params = event.get("value", event)

    return run_action(params)


# ============================================================
# Original HTTP/OpenWhisk interface
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

                self._send_json(200, result)

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
# Unix-domain socket server for runc + CRIU
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
# Start application
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
            "Graph BFS HTTP server listening on port 8080",
            flush=True
        )

        server.serve_forever()

    else:

        control_thread.join()
