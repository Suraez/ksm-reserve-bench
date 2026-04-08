import json
import os
import subprocess
import tempfile
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer

INIT_DONE = False

IMAGE_NAME = "watermark.png"
VIDEO_NAME = "hi_chitanda_eru.mp4"
LOCAL_PATH = "/action"


def call_ffmpeg(args):
    ret = subprocess.run(
        ["ffmpeg", "-y"] + args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    if ret.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {ret.stdout}")


def to_video(duration: int, local_path: str):
    input_video = os.path.join(local_path, VIDEO_NAME)
    input_image = os.path.join(local_path, IMAGE_NAME)
    output = "processed_hi_chitanda_eru.mp4"
    output_path = os.path.join(tempfile.gettempdir(), output)

    if not os.path.isfile(input_video):
        raise FileNotFoundError(f"Video file not found: {input_video}")
    if not os.path.isfile(input_image):
        raise FileNotFoundError(f"Image file not found: {input_image}")

    if os.path.exists(output_path):
        os.remove(output_path)

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

    output_size = os.path.getsize(output_path)

    return {
        "result": f"Video {output} finished!",
        "output_name": output,
        "output_path": output_path,
        "output_size_bytes": output_size,
        "output_size_mib": round(output_size / (1024 * 1024), 2),
    }


def run_action(params):
    duration = int(params.get("duration", 5))
    keep_output = bool(params.get("keep_output", True))
    local_path = params.get("local_path", LOCAL_PATH)

    response = to_video(duration, local_path)

    if not keep_output and os.path.exists(response["output_path"]):
        os.remove(response["output_path"])
        response["output_removed"] = True
    else:
        response["output_removed"] = False

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