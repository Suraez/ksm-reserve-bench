import json
import os
import subprocess
import tempfile
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer

DEFAULT_VIDEO_FILENAME = "hi_chitanda_eru.mp4"
DEFAULT_IMAGE_FILENAME = "watermark.png"
DEFAULT_LOCAL_PATH = "/action"

INIT_DONE = False


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


def require_file(path: str, label: str):
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Required {label} file not found: {path}")


def to_video(duration: int, input_video: str, input_image: str, output_name: str):
    require_file(input_video, "video")
    require_file(input_image, "image")

    output_path = os.path.join(tempfile.gettempdir(), output_name)

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
        output_path,
    ])

    return output_path


def run_action(params):
    video_filename = params.get("video_filename", DEFAULT_VIDEO_FILENAME)
    image_filename = params.get("image_filename", DEFAULT_IMAGE_FILENAME)
    local_path = params.get("local_path", DEFAULT_LOCAL_PATH)
    duration = int(params.get("duration", 5))
    keep_output = bool(params.get("keep_output", True))

    input_video = os.path.join(local_path, video_filename)
    input_image = os.path.join(local_path, image_filename)

    video_base = os.path.splitext(os.path.basename(video_filename))[0]
    output_name = f"processed_{video_base}.mp4"

    output_path = to_video(duration, input_video, input_image, output_name)
    output_size = os.path.getsize(output_path)

    response = {
        "result": f"Video {video_filename}+{image_filename} finished!",
        "input_video": input_video,
        "input_image": input_image,
        "output_name": output_name,
        "output_path": output_path,
        "output_size_bytes": output_size,
        "output_size_mib": round(output_size / (1024 * 1024), 2),
    }

    if not keep_output and os.path.exists(output_path):
        os.remove(output_path)
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
