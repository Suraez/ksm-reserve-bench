
import ctypes
import gc
import json
import os
import shutil
import socket
import tempfile
import threading
import traceback

from http.server import BaseHTTPRequestHandler, HTTPServer


# ============================================================
# Configuration
# ============================================================

INIT_DONE = False

FOLDER_NAME = "acmart-master"
LOCAL_PATH = "/action"

CONTROL_SOCKET = "/control/invoke.sock"

ENABLE_HTTP = (
    os.environ.get("COMPRESSION_ENABLE_HTTP", "0") == "1"
)


# ============================================================
# libc setup for memory purging
#
# KSM is enabled separately by ksm_wrap.c using
# PR_SET_MEMORY_MERGE.
# No Python madvise() is needed.
# ============================================================

libc = ctypes.CDLL("libc.so.6", use_errno=True)

libc.malloc_trim.argtypes = [ctypes.c_size_t]
libc.malloc_trim.restype = ctypes.c_int


def purge_memory():
    collected = gc.collect()
    trimmed = libc.malloc_trim(0)

    return {
        "gc_collected": collected,
        "malloc_trim_result": trimmed,
    }


# ============================================================
# Original File Compression functionality
# ============================================================

def compress(path: str, key: str):
    source_dir = os.path.join(path, key)

    if not os.path.isdir(source_dir):
        raise FileNotFoundError(
            f"Source directory not found: {source_dir}"
        )

    archive_base = os.path.join(tempfile.gettempdir(), key)
    archive_path = f"{archive_base}.zip"

    if os.path.exists(archive_path):
        os.remove(archive_path)

    shutil.make_archive(
        archive_base,
        "zip",
        root_dir=source_dir
    )

    archive_name = f"{key}.zip"
    archive_size = os.path.getsize(archive_path)

    return archive_name, archive_size, archive_path


def folder_stats(path: str, key: str):
    source_dir = os.path.join(path, key)

    total_files = 0
    total_bytes = 0

    for root, _, files in os.walk(source_dir):
        for name in files:
            total_files += 1
            total_bytes += os.path.getsize(
                os.path.join(root, name)
            )

    return {
        "source_dir": source_dir,
        "total_files": total_files,
        "total_bytes": total_bytes,
        "total_mib": round(
            total_bytes / (1024 * 1024), 2
        ),
    }


# ============================================================
# Compression action + post-invocation purging
# ============================================================

def run_action(params):
    if params is None:
        params = {}

    if not isinstance(params, dict):
        raise ValueError(
            "Request parameters must be a JSON object"
        )

    folder_name = params.get(
        "folder_name", FOLDER_NAME
    )
    local_path = params.get(
        "local_path", LOCAL_PATH
    )
    keep_archive = bool(
        params.get("keep_archive", True)
    )

    archive_name, archive_size, archive_path = compress(
        local_path, folder_name
    )

    response = {
        "result": (
            f"{archive_name} compression "
            f"in size {archive_size} finished!"
        ),
        "archive_name": archive_name,
        "archive_size_bytes": archive_size,
        "archive_size_mib": round(
            archive_size / (1024 * 1024), 2
        ),
        "archive_path": archive_path,
        "stats": folder_stats(local_path, folder_name),
    }

    # Preserve the existing archive retention behavior.
    if not keep_archive and os.path.exists(archive_path):
        os.remove(archive_path)
        response["archive_removed"] = True
    else:
        response["archive_removed"] = False

    # Free reclaimable Python and glibc heap memory.
    response["purge"] = purge_memory()

    return response


# ============================================================
# Accept direct and OpenWhisk-style input
# ============================================================

def execute_event(event):
    if not isinstance(event, dict):
        raise ValueError(
            "Invocation must be a JSON object"
        )

    params = event.get("value", event)

    return run_action(params)


# ============================================================
# Existing HTTP/OpenWhisk handler
# ============================================================

class OpenWhiskHandler(BaseHTTPRequestHandler):

    def _send_json(self, code, payload):
        body = json.dumps(payload).encode("utf-8")

        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
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
                404, {"error": "not found"}
            )

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
                    "init_done": INIT_DONE
                }
            )
            return

        self._send_json(
            404, {"error": "not found"}
        )

    def log_message(self, format, *args):
        return


# ============================================================
# Unix-domain socket control server
# Used for runc + CRIU cloning
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
                "traceback": traceback.format_exc(),
            }

        try:
            conn.sendall(
                (json.dumps(response) + "\n").encode("utf-8")
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
            "File Compression HTTP server "
            "listening on port 8080",
            flush=True
        )
        server.serve_forever()

    else:
        control_thread.join()
