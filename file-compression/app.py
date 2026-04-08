import json
import os
import shutil
import tempfile
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer

INIT_DONE = False

FOLDER_NAME = "acmart-master"
LOCAL_PATH = "/action"


def compress(path: str, key: str):
    source_dir = os.path.join(path, key)
    if not os.path.isdir(source_dir):
        raise FileNotFoundError(f"Source directory not found: {source_dir}")

    archive_base = os.path.join(tempfile.gettempdir(), key)
    archive_path = f"{archive_base}.zip"

    if os.path.exists(archive_path):
        os.remove(archive_path)

    shutil.make_archive(archive_base, "zip", root_dir=source_dir)

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
            total_bytes += os.path.getsize(os.path.join(root, name))

    return {
        "source_dir": source_dir,
        "total_files": total_files,
        "total_bytes": total_bytes,
        "total_mib": round(total_bytes / (1024 * 1024), 2),
    }


def run_action(params):
    folder_name = params.get("folder_name", FOLDER_NAME)
    local_path = params.get("local_path", LOCAL_PATH)
    keep_archive = bool(params.get("keep_archive", True))

    archive_name, archive_size, archive_path = compress(local_path, folder_name)

    response = {
        "result": f"{archive_name} compression in size {archive_size} finished!",
        "archive_name": archive_name,
        "archive_size_bytes": archive_size,
        "archive_size_mib": round(archive_size / (1024 * 1024), 2),
        "archive_path": archive_path,
        "stats": folder_stats(local_path, folder_name),
    }

    if not keep_archive and os.path.exists(archive_path):
        os.remove(archive_path)
        response["archive_removed"] = True
    else:
        response["archive_removed"] = False

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