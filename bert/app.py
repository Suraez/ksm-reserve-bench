import json
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer

import torch
from transformers import BertModel, BertTokenizer

torch.set_num_threads(1)

INIT_DONE = False

# Load once at module import so warm invocations reuse the model
tokenizer = BertTokenizer.from_pretrained("bert-base-uncased")
model = BertModel.from_pretrained("bert-base-uncased").eval()


def run_action(params):
    body = params.get("body", "")

    if isinstance(body, bytes):
        text = body.decode("utf-8")
    elif isinstance(body, str):
        text = body
    elif isinstance(body, dict):
        text = body.get("text", "")
    else:
        text = str(body)

    encoded_input = tokenizer(text + " [MASK]", return_tensors="pt")

    with torch.no_grad():
        output = model(**encoded_input)

    shape_str = str(tuple(output.last_hidden_state.shape))

    return {
        "statusCode": 200,
        "message": shape_str,
        "text": text,
        "model_name": "bert-base-uncased",
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
                    "statusCode": 500,
                    "output_tensor": str(exc),
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