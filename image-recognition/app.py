import json
import os
import re
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

import requests
import torch
from PIL import Image
from torchvision import transforms
from torchvision.models import resnet50

INIT_DONE = False

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


def safe_filename(filename: str, default: str = "input.jpg") -> str:
    """
    Prevent paths like ../../etc/passwd from being used as filenames.
    Keeps only simple filename characters.
    """
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


def download(url: str, directory: str, filename: str, overwrite: bool = False) -> str:
    """
    Download a file into directory/filename.

    By default, cached files are reused.
    If overwrite=True, the file is downloaded again.
    """
    os.makedirs(directory, exist_ok=True)

    filename = safe_filename(filename)
    path = os.path.join(directory, filename)

    if overwrite or not os.path.isfile(path):
        response = requests.get(url, timeout=60)
        response.raise_for_status()

        with open(path, "wb") as f:
            f.write(response.content)

    return path


def ensure_labels():
    global idx2label

    if idx2label is None:
        dataset_path = download(
            DATASET_URL,
            LOCAL_PATH,
            DATASET_NAME,
            overwrite=False,
        )

        with open(dataset_path, "r") as f:
            class_idx = json.load(f)

        idx2label = [class_idx[str(k)][1] for k in range(len(class_idx))]

    return idx2label


def ensure_model():
    global model

    if model is None:
        model_path = download(
            MODEL_URL,
            LOCAL_PATH,
            MODEL_NAME,
            overwrite=False,
        )

        model = resnet50(pretrained=False)
        state_dict = torch.load(model_path, map_location="cpu")
        model.load_state_dict(state_dict)
        model.eval()

    return model


def get_image_from_params(params: dict) -> tuple[str, str, str]:
    """
    Reads runtime input.

    Supported input:
    {
      "value": {
        "image_url": "https://example.com/cat.jpg",
        "image_name": "cat.jpg"
      }
    }

    image_name is optional.
    If omitted, it is inferred from the URL.
    """

    image_url = params.get("image_url", DEFAULT_IMAGE_URL)

    image_name = params.get("image_name")
    if not image_name:
        image_name = infer_filename_from_url(image_url, DEFAULT_IMAGE_NAME)

    image_name = safe_filename(image_name, DEFAULT_IMAGE_NAME)

    # For user-provided runtime images, use /tmp so input files are not baked into the image.
    # For the default image, also using /tmp is fine.
    image_path = download(
        image_url,
        INPUT_CACHE_PATH,
        image_name,
        overwrite=params.get("overwrite", False),
    )

    return image_path, image_url, image_name


def run_action(params):
    if params is None:
        params = {}

    if not isinstance(params, dict):
        raise ValueError("Request value must be a JSON object")

    labels = ensure_labels()
    current_model = ensure_model()

    image_path, request_image_url, request_image_name = get_image_from_params(params)

    input_image = Image.open(image_path).convert("RGB")

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

    return {
        "result": f"Prediction: index {prediction_index}, class {prediction_label}",
        "prediction_index": prediction_index,
        "prediction_label": prediction_label,
        "image_name": request_image_name,
        "image_url": request_image_url,
        "image_path_inside_container": image_path,
        "model_name": MODEL_NAME,
        "dataset_name": DATASET_NAME,
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

                # Optional warm-up: load model and labels during init.
                ensure_labels()
                ensure_model()

                INIT_DONE = True
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "init_done": INIT_DONE,
                        "message": "Model and labels loaded",
                    },
                )
                return

            if self.path == "/run":
                payload = self._read_json()

                # Supports both:
                # { "value": { "image_url": "...", "image_name": "..." } }
                # and:
                # { "image_url": "...", "image_name": "..." }
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
            self._send_json(
                200,
                {
                    "status": "ok",
                    "init_done": INIT_DONE,
                },
            )
            return

        self._send_json(404, {"error": "not found"})

    def log_message(self, format, *args):
        return


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", 8080), OpenWhiskHandler)
    print("Listening on 0.0.0.0:8080", flush=True)
    server.serve_forever()
