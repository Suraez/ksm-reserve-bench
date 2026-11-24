
import ctypes
import gc
import json
import os
import socket
import tempfile
import threading
import traceback

from http.server import BaseHTTPRequestHandler, HTTPServer

from squiggle import transform


# ============================================================
# Configuration
# ============================================================

DEFAULT_FILENAME1 = "gene1.txt"
DEFAULT_FILENAME2 = "gene2.txt"
DEFAULT_LOCAL_PATH = "/action"

CONTROL_SOCKET = "/control/invoke.sock"

DNA_ENABLE_HTTP = (
    os.environ.get("DNA_ENABLE_HTTP", "0") == "1"
)

INIT_DONE = False


# ============================================================
# libc configuration
#
# Process-wide KSM eligibility is handled by ksm_wrap.c:
#
# prctl(PR_SET_MEMORY_MERGE, 1, 0, 0, 0)
#
# No Python madvise(MADV_MERGEABLE) is needed.
# ============================================================

libc = ctypes.CDLL(
    "libc.so.6",
    use_errno=True
)

libc.malloc_trim.argtypes = [
    ctypes.c_size_t
]

libc.malloc_trim.restype = ctypes.c_int


# ============================================================
# Memory purging after each invocation
# ============================================================

def purge_memory():

    collected = gc.collect()

    trimmed = libc.malloc_trim(0)

    return {
        "gc_collected": collected,
        "malloc_trim_result": trimmed
    }


# ============================================================
# Original DNA Visualization functionality
# ============================================================

def read_text_file(path: str) -> str:

    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"Required input file not found: {path}"
        )

    with open(
        path,
        "r",
        encoding="utf-8"
    ) as f:
        return f.read()


def visualize(data1: str, data2: str):

    return (
        transform(data1) +
        transform(data2)
    )


# ============================================================
# DNA Visualization action
# ============================================================

def run_action(params):

    if params is None:
        params = {}

    if not isinstance(params, dict):
        raise ValueError(
            "Request parameters must be a JSON object"
        )

    filename1 = params.get(
        "filename1",
        DEFAULT_FILENAME1
    )

    filename2 = params.get(
        "filename2",
        DEFAULT_FILENAME2
    )

    local_path = params.get(
        "local_path",
        DEFAULT_LOCAL_PATH
    )

    keep_output = bool(
        params.get("keep_output", True)
    )

    input_path1 = os.path.join(
        local_path,
        filename1
    )

    input_path2 = os.path.join(
        local_path,
        filename2
    )

    # ========================================================
    # Read input files
    # ========================================================

    data1 = read_text_file(input_path1)
    data2 = read_text_file(input_path2)

    # ========================================================
    # Original computation
    # ========================================================

    result = visualize(data1, data2)

    # ========================================================
    # Write result to /tmp/result.txt
    # ========================================================

    output_path = os.path.join(
        tempfile.gettempdir(),
        "result.txt"
    )

    with open(output_path, "wb") as out:
        out.write(
            json.dumps(result).encode("utf-8")
        )

    output_size = os.path.getsize(
        output_path
    )

    response = {
        "result": (
            f"Visualize {filename1}+{filename2} finished!"
        ),
        "input_1": input_path1,
        "input_2": input_path2,
        "output_path": output_path,
        "output_size_bytes": output_size,
    }

    # ========================================================
    # Preserve original keep_output behavior
    # ========================================================

    if not keep_output and os.path.exists(output_path):

        os.remove(output_path)

        response["output_removed"] = True

    else:

        response["output_removed"] = False

    # ========================================================
    # Release invocation-specific Python objects
    # ========================================================

    del data1
    del data2
    del result

    # ========================================================
    # Purge memory after invocation
    # ========================================================

    response["purge"] = purge_memory()

    return response


# ============================================================
# Input compatibility
#
# Supports:
#
# {"filename1": "gene1.txt"}
#
# and:
#
# {"value": {"filename1": "gene1.txt"}}
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

        body = json.dumps(
            payload
        ).encode("utf-8")

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

        raw = self.rfile.read(
            length
        ).decode("utf-8")

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
#
# Used by native runc + CRIU clones.
#
# Each clone has a different host-side mount directory
# but the same in-container socket path:
#
# /control/invoke.sock
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
                        "Invocation request exceeds 1 MiB"
                    )

                chunks.append(data)

                if b"\n" in data:
                    break

            request = b"".join(chunks)

            request = request.split(
                b"\n",
                1
            )[0]

            event = json.loads(
                request.decode("utf-8")
            )

            result = execute_event(event)

            response = {
                **result
            }

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

    # Remove stale socket from a previous run.
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

    if DNA_ENABLE_HTTP:

        server = HTTPServer(
            ("0.0.0.0", 8080),
            OpenWhiskHandler
        )

        print(
            "DNA Visualization HTTP server "
            "listening on port 8080",
            flush=True
        )

        server.serve_forever()

    else:

        # Unix-only mode for runc + CRIU.
        control_thread.join()
