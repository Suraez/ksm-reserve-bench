import json
import os
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
import torch
from PIL import Image
from torchvision import transforms
from torchvision.models import resnet50

INIT_DONE = False

image_name = "tesla.jpg"
image_url = "https://github.com/spcl/serverless-benchmarks-data/blob/6a17a460f289e166abb47ea6298fb939e80e8beb/400.inference/411.image-recognition/fake-resnet/800px-20180630_Tesla_Model_S_70D_2015_midnight_blue_left_front.jpg?raw=true"

model_name = "resnet50.pth"
model_url = "https://github.com/spcl/serverless-benchmarks-data/blob/6a17a460f289e166abb47ea6298fb939e80e8beb/400.inference/411.image-recognition/model/resnet50-19c8e357.pth?raw=true"

dataset_name = "imagenet_class_index.json"
dataset_url = "https://github.com/spcl/serverless-benchmarks/blob/master/benchmarks/400.inference/411.image-recognition/python/imagenet_class_index.json?raw=true"

local_path = "/action/"

model = None
idx2label = None


def download(url, local_path, filename):
    path = os.path.join(local_path, filename)
    if not os.path.isfile(path):
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        with open(path, "wb") as f:
            f.write(r.content)
    return path


def ensure_labels():
    global idx2label
    if idx2label is None:
        dataset_path = download(dataset_url, local_path, dataset_name)
        class_idx = json.load(open(dataset_path, "r"))
        idx2label = [class_idx[str(k)][1] for k in range(len(class_idx))]
    return idx2label


def ensure_model():
    global model
    if model is None:
        model_path = download(model_url, local_path, model_name)
        model = resnet50(pretrained=False)
        model.load_state_dict(torch.load(model_path, map_location="cpu"))
        model.eval()
    return model


def run_action(params):
    labels = ensure_labels()
    image_path = download(image_url, local_path, image_name)
    current_model = ensure_model()

    input_image = Image.open(image_path).convert("RGB")
    preprocess = transforms.Compose([
        transforms.Resize(256),
        transforms.CenterCrop(224),
        transforms.ToTensor(),
        transforms.Normalize(mean=[0.485, 0.456, 0.406],
                             std=[0.229, 0.224, 0.225]),
    ])
    input_tensor = preprocess(input_image)
    input_batch = input_tensor.unsqueeze(0)

    with torch.no_grad():
        output = current_model(input_batch)
        _, index = torch.max(output, 1)

    prediction_index = int(index.item())
    prediction_label = labels[prediction_index]
    results = f"Prediction: index {prediction_index}, class {prediction_label}"

    return {
        "result": results,
        "prediction_index": prediction_index,
        "prediction_label": prediction_label,
        "image_name": image_name,
        "model_name": model_name,
        "dataset_name": dataset_name,
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
        self._send_json(404, {"error": "not found"})

    def log_message(self, format, *args):
        return


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", 8080), OpenWhiskHandler)
    print("Listening on 0.0.0.0:8080", flush=True)
    server.serve_forever()