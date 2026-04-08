import json
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer

import igraph

INIT_DONE = False

GRAPH = None
GRAPH_SIZE = None
GRAPH_M = None


def build_graph(size: int, m: int):
    return igraph.Graph.Barabasi(size, m)


def ensure_graph(size: int, m: int):
    global GRAPH, GRAPH_SIZE, GRAPH_M

    if GRAPH is None or GRAPH_SIZE != size or GRAPH_M != m:
        GRAPH = build_graph(size, m)
        GRAPH_SIZE = size
        GRAPH_M = m

    return GRAPH


def graph_ops(size: int, m: int):
    graph = ensure_graph(size, m)
    return float(graph.pagerank()[0])


def graph_stats():
    return {
        "graph_initialized": GRAPH is not None,
        "graph_size": GRAPH_SIZE,
        "graph_m": GRAPH_M,
        "vertices": GRAPH.vcount() if GRAPH is not None else 0,
        "edges": GRAPH.ecount() if GRAPH is not None else 0,
    }


def run_action(params):
    size = int(params.get("size", 1000))
    m = int(params.get("m", 10))

    result = graph_ops(size, m)

    return {
        "result": f"{size} size graph PageRank finished!",
        "pagerank_first_value": result,
        "size": size,
        "m": m,
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