
import ctypes
import gc
import json
import os
import re
import socket
import threading
import traceback

from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

import requests
import torch
from PIL import Image
from torchvision import transforms
from torchvision.models import resnet50


torch.set_num_threads(1)

INIT_DONE = False

CONTROL_SOCKET = "/control/invoke.sock"
ENABLE_HTTP = os.environ.get("IMAGE_ENABLE_HTTP", "1") == "1"


# ============================================================
# Original Image Recognition configuration
# ============================================================

DEFAULT_IMAGE_NAME = "tesla.jpg"
DEFAULT_IMAGE_URL = (
    "https://github.com/spcl/serverless-benchmarks-data/blob/"
    "6a17a460f289e166abb47ea6298fb939e80e8beb/"
    "400.inference/411.image-recognition/fake-resnet/"
    "800px-20180630_Tesla_Model_S_70D_2015_midnight_blue_left_front.jpg?raw=true"
)

MODEL_NAME = "resnet50.pth"
MODEL_URL = (
    "https://github.com/spcl/serverless-benchmarks-data/blob/"
    "6a17a460f289e166abb47ea6298fb939e80e8beb/"
    "400.inference/411.image-recognition/model/resnet50-19c8e357.pth?raw=true"
)

DATASET_NAME = "imagenet_class_index.json"
DATASET_URL = (
    "https://github.com/spcl/serverless-benchmarks/blob/master/"
    "benchmarks/400.inference/411.image-recognition/python/"
    "imagenet_class_index.json?raw=true"
)

LOCAL_PATH = "/action"
INPUT_CACHE_PATH = "/tmp/action-inputs"

model = None
idx2label = None


# ============================================================
# libc setup: malloc_trim only
#
# KSM eligibility is enabled by ksm_wrap.c using:
# prctl(PR_SET_MEMORY_MERGE, 1, 0, 0, 0)
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
# File handling
# ============================================================

def safe_filename(filename: str, default: str = "input.jpg") -> str:

    if not filename:
        return default

    filename = os.path.basename(filename)
    filename = re.sub(r"[^a-zA-Z0-9._-]", "_", filename)

    if not filename:
        return default

    return filename


def infer_filename_from_url(url: str, default: str = "input.jpg") -> str:

    try:
        parsed = urlparse(url)
        candidate = os.path.basename(parsed.path)

        return safe_filename(candidate, default)

    except Exception:
        return default


# ============================================================
# Download and close HTTP connection
#
# Important for CRIU checkpointing:
# Do not retain unnecessary TCP connections after downloads.
# ============================================================

def download(url, directory, filename, overwrite=False):

    os.makedirs(directory, exist_ok=True)

    filename = safe_filename(filename)
    path = os.path.join(directory, filename)

    if overwrite or not os.path.isfile(path):

        with requests.get(
            url,
            timeout=60,
            stream=True
        ) as response:

            response.raise_for_status()

            with open(path, "wb") as f:

                for chunk in response.iter_content(
                    chunk_size=1024 * 1024
                ):
                    if chunk:
                        f.write(chunk)

    return path


# ============================================================
# Load ImageNet labels once
# ============================================================

def ensure_labels():

    global idx2label

    if idx2label is None:

        dataset_path = download(
            DATASET_URL,
            LOCAL_PATH,
            DATASET_NAME,
            overwrite=False
        )

        with open(dataset_path, "r") as f:
            class_idx = json.load(f)

        idx2label = [
            class_idx[str(k)][1]
            for k in range(len(class_idx))
        ]

        del class_idx

    return idx2label


# ============================================================
# Load ResNet50 once
# ============================================================

def ensure_model():

    global model

    if model is None:

        model_path = download(
            MODEL_URL,
            LOCAL_PATH,
            MODEL_NAME,
            overwrite=False
        )

        loaded_model = resnet50(pretrained=False)

        state_dict = torch.load(
            model_path,
            map_location="cpu"
        )

        loaded_model.load_state_dict(state_dict)
        loaded_model.eval()

        # The model now owns the loaded parameters.
        del state_dict

        model = loaded_model

    return model


# ============================================================
# Get image input
# ============================================================

def get_image_from_params(params):

    image_url = params.get(
        "image_url",
        DEFAULT_IMAGE_URL
    )

    image_name = params.get("image_name")

    if not image_name:
        image_name = infer_filename_from_url(
            image_url,
            DEFAULT_IMAGE_NAME
        )

    image_name = safe_filename(
        image_name,
        DEFAULT_IMAGE_NAME
    )

    image_path = download(
        image_url,
        INPUT_CACHE_PATH,
        image_name,
        overwrite=params.get("overwrite", False)
    )

    return image_path, image_url, image_name


# ============================================================
# Original Image Recognition benchmark
# ============================================================

def run_action(params):

    if params is None:
        params = {}

    if not isinstance(params, dict):
        raise ValueError(
            "Request value must be a JSON object"
        )

    # Load and retain these across warm invocations.
    labels = ensure_labels()
    current_model = ensure_model()

    image_path, request_image_url, request_image_name = (
        get_image_from_params(params)
    )

    # Explicitly close the input image file.
    with Image.open(image_path) as img:
        input_image = img.convert("RGB")

    preprocess = transforms.Compose(
        [
            transforms.Resize(256),
            transforms.CenterCrop(224),
            transforms.ToTensor(),
            transforms.Normalize(
                mean=[0.485, 0.456, 0.406],
                std=[0.229, 0.224, 0.225],
            ),
        ]
    )

    input_tensor = preprocess(input_image)
    input_batch = input_tensor.unsqueeze(0)

    with torch.no_grad():

        output = current_model(input_batch)
        _, index = torch.max(output, 1)

    prediction_index = int(index.item())
    prediction_label = labels[prediction_index]

    result = {
        "result": (
            f"Prediction: index {prediction_index}, "
            f"class {prediction_label}"
        ),
        "prediction_index": prediction_index,
        "prediction_label": prediction_label,
        "image_name": request_image_name,
        "image_url": request_image_url,
        "image_path_inside_container": image_path,
        "model_name": MODEL_NAME,
        "dataset_name": DATASET_NAME,
    }

    # ========================================================
    # Release invocation-created objects
    #
    # Do not delete the global model or labels.
    # They must remain warm for CRIU checkpointing.
    # ========================================================

    del output
    del index
    del input_batch
    del input_tensor
    del input_image
    del preprocess

    # ========================================================
    # Purge reclaimable memory after inference
    # ========================================================

    result["purge"] = purge_memory()

    return result


# ============================================================
# Accept direct and OpenWhisk-style requests
# ============================================================

def execute_event(event):

    if not isinstance(event, dict):
        raise ValueError(
            "Invocation must be a JSON object"
        )

    params = event.get("value", event)

    if not isinstance(params, dict):
        raise ValueError(
            "Invocation parameters must be a JSON object"
        )

    return run_action(params)


# ============================================================
# Existing HTTP/OpenWhisk server
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

                ensure_labels()
                ensure_model()

                INIT_DONE = True

                self._send_json(
                    200,
                    {
                        "ok": True,
                        "init_done": INIT_DONE,
                        "message": "Model and labels loaded"
                    }
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
                    "init_done": INIT_DONE,
                    "model_loaded": model is not None
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
# Unix-domain control server
#
# Used for runc + CRIU warm-parent cloning.
#
# Each restored clone has its own host-side control directory,
# but inside the container the socket remains:
#
# /control/invoke.sock
# ============================================================

def unix_control_server():

    os.makedirs(
        os.path.dirname(CONTROL_SOCKET),
        exist_ok=True
    )

    # Remove a stale socket file at fresh startup.
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
        "Unix control server listening on {}".format(
            CONTROL_SOCKET
        ),
        flush=True
    )

    while True:

        conn, _ = server.accept()

        try:

            # Client sends: <JSON>\n
            # Server returns: <JSON>\n

            chunks = []

            while True:

                data = conn.recv(65536)

                if not data:
                    break

                chunks.append(data)

                if b"\n" in data:
                    break

            request = b"".join(chunks)
            request = request.split(b"\n", 1)[0]

            event = json.loads(
                request.decode("utf-8")
            )

            # Execute ResNet50 in this restored process.
            result = execute_event(event)

            response = (
                json.dumps(result) + "\n"
            ).encode("utf-8")

            conn.sendall(response)

        except Exception as exc:

            response = (
                json.dumps({
                    "error": str(exc),
                    "traceback": traceback.format_exc()
                }) + "\n"
            ).encode("utf-8")

            try:
                conn.sendall(response)
            except OSError:
                pass

        finally:
            conn.close()


# ============================================================
# Start servers
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
            "Image Recognition HTTP server "
            "listening on port 8080",
            flush=True
        )

        server.serve_forever()

    else:

        # Unix-only mode for native runc clones.
        control_thread.join()
