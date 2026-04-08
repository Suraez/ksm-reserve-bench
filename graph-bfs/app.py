import json
import traceback
from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np

INIT_DONE = False

# Global retained graph so memory stays alive across warm invocations
INDPTR = None
INDICES = None
GRAPH_NODES = None
GRAPH_DEGREE = None


def build_deterministic_csr_graph(num_nodes: int, degree: int):
    """
    Build a deterministic directed graph in CSR form.

    Each node i connects to:
      (i + 1) % N, (i + 2) % N, ... (i + degree) % N
    """
    global INDPTR, INDICES, GRAPH_NODES, GRAPH_DEGREE

    if INDPTR is not None and GRAPH_NODES == num_nodes and GRAPH_DEGREE == degree:
        return

    total_edges = num_nodes * degree

    indptr = np.zeros(num_nodes + 1, dtype=np.int64)
    indices = np.zeros(total_edges, dtype=np.int32)

    edge_pos = 0
    for i in range(num_nodes):
        indptr[i] = edge_pos
        for j in range(1, degree + 1):
            indices[edge_pos] = (i + j) % num_nodes
            edge_pos += 1
    indptr[num_nodes] = edge_pos

    INDPTR = indptr
    INDICES = indices
    GRAPH_NODES = num_nodes
    GRAPH_DEGREE = degree


def bfs_csr(start_node: int = 0):
    global INDPTR, INDICES, GRAPH_NODES

    visited = np.zeros(GRAPH_NODES, dtype=np.uint8)
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
        "mb_retained": round(total_bytes / (1024 * 1024), 2),
        "indptr_bytes": int(INDPTR.nbytes) if INDPTR is not None else 0,
        "indices_bytes": int(INDICES.nbytes) if INDICES is not None else 0,
    }


def run_action(params):
    num_nodes = int(params.get("num_nodes", 5_000_000))
    degree = int(params.get("degree", 10))
    start_node = int(params.get("start_node", 0))

    build_deterministic_csr_graph(num_nodes, degree)
    visited_count = bfs_csr(start_node)

    return {
        "message": f"BFS finished for graph with {num_nodes} nodes, degree {degree}",
        "visited_count": visited_count,
        "start_node": start_node,
        "stats": graph_stats(),
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
        if self.path == "/stats":
            self._send_json(200, graph_stats())
            return
        self._send_json(404, {"error": "not found"})

    def log_message(self, format, *args):
        return


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", 8080), OpenWhiskHandler)
    print("Listening on 0.0.0.0:8080", flush=True)
    server.serve_forever()