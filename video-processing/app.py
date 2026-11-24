
import ctypes
import gc
import json
import os
import socket
import subprocess
import tempfile
import threading
import traceback

from http.server import BaseHTTPRequestHandler, HTTPServer


# ============================================================
# Configuration
# ============================================================

DEFAULT_VIDEO_FILENAME = "hi_chitanda_eru.mp4"
DEFAULT_IMAGE_FILENAME = "watermark.png"
DEFAULT_LOCAL_PATH = "/action"

CONTROL_SOCKET = "/control/invoke.sock"

ENABLE_HTTP = (
    os.environ.get("VP_ENABLE_HTTP", "0") == "1"
)

INIT_DONE = False


# ============================================================
# Memory purging
#
# KSM eligibility is enabled by ksm_wrap.c.
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
        "malloc_trim_result": trimmed
    }


# ============================================================
# Original FFmpeg functionality
# ============================================================

def call_ffmpeg(args):

    ret = subprocess.run(
        ["ffmpeg", "-y"] + args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True
    )

    if ret.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed: {ret.stdout}"
        )


def require_file(path: str, label: str):

    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"Required {label} file not found: {path}"
        )


def to_video(
    duration: int,
    input_video: str,
    input_image: str,
    output_name: str
):

    require_file(input_video, "video")
    require_file(input_image, "image")

    output_path = os.path.join(
        tempfile.gettempdir(),
        output_name
    )

    if os.path.exists(output_path):
        os.remove(output_path)

    # Same FFmpeg processing pipeline as original.
    call_ffmpeg([
        "-i", input_video,
        "-i", input_image,
        "-t", str(duration),
        "-filter_complex",
        "[0]trim=start_frame=0:end_frame=50[v0];"
        "[0]trim=start_frame=100:end_frame=150[v1];"
        "[v0][v1]concat=n=2[v2];"
        "[1]hflip[v3];"
        "[v2][v3]overlay=eof_action=repeat[v4];"
        "[v4]drawbox=50:50:120:120:red:t=5[v5]",
        "-map", "[v5]",
        output_path
    ])

    return output_path


# ============================================================
# Video Processing invocation
# ============================================================

def run_action(params):

    if params is None:
        params = {}

    if not isinstance(params, dict):
        raise ValueError(
            "Invocation parameters must be a JSON object"
        )

    video_filename = params.get(
        "video_filename",
        DEFAULT_VIDEO_FILENAME
    )

    image_filename = params.get(
        "image_filename",
        DEFAULT_IMAGE_FILENAME
    )

    local_path = params.get(
        "local_path",
        DEFAULT_LOCAL_PATH
    )

    duration = int(params.get("duration", 5))

    # Preserve original default.
    keep_output = bool(
        params.get("keep_output", True)
    )

    if duration <= 0:
        raise ValueError(
            "duration must be positive"
        )

    input_video = os.path.join(
        local_path,
        video_filename
    )

    input_image = os.path.join(
        local_path,
        image_filename
    )

    video_base = os.path.splitext(
        os.path.basename(video_filename)
    )[0]

    output_name = f"processed_{video_base}.mp4"

    # FFmpeg runs to completion before proceeding.
    output_path = to_video(
        duration,
        input_video,
        input_image,
        output_name
    )

    output_size = os.path.getsize(output_path)

    response = {
        "result": (
            f"Video {video_filename}+"
            f"{image_filename} finished!"
        ),
        "input_video": input_video,
        "input_image": input_image,
        "output_name": output_name,
        "output_path": output_path,
        "output_size_bytes": output_size,
        "output_size_mib": round(
            output_size / (1024 * 1024),
            2
        )
    }

    # Remove generated output when requested.
    if not keep_output and os.path.exists(output_path):

        os.remove(output_path)
        response["output_removed"] = True

    else:
        response["output_removed"] = False

    # Release eligible temporary Python heap memory.
    response["purge"] = purge_memory()

    return response


# ============================================================
# Support OpenWhisk and direct JSON payloads
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
            "Video Processing HTTP server listening on port 8080",
            flush=True
        )

        server.serve_forever()

    else:

        control_thread.join()
