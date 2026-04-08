const fs = require("fs");
const path = require("path");
const http = require("http");
const tesseract = require("tesseractocr");

const filename = "exodia.png";
const localPath = "/action/src";
let initDone = false;

function ocrFromFile(filePath) {
  return new Promise((resolve, reject) => {
    if (!fs.existsSync(filePath)) {
      reject(new Error(`Input file not found: ${filePath}`));
      return;
    }

    const imageBuffer = fs.readFileSync(filePath);

    tesseract.recognize(imageBuffer, (err, text) => {
      if (err) {
        reject(err);
      } else {
        resolve(text);
      }
    });
  });
}

async function runAction(params = {}) {
  const inputFilename = params.filename || filename;
  const inputPath = params.local_path || localPath;
  const filePath = path.join(inputPath, inputFilename);

  const text = await ocrFromFile(filePath);

  return {
    result: `OCR ${inputFilename} finished!`,
    file_path: filePath,
    extracted_text: text,
    extracted_text_length: text.length
  };
}

function sendJson(res, statusCode, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(statusCode, {
    "Content-Type": "application/json",
    "Content-Length": Buffer.byteLength(body)
  });
  res.end(body);
}

function readJson(req) {
  return new Promise((resolve, reject) => {
    let raw = "";

    req.on("data", chunk => {
      raw += chunk;
    });

    req.on("end", () => {
      if (!raw.trim()) {
        resolve({});
        return;
      }

      try {
        resolve(JSON.parse(raw));
      } catch (err) {
        reject(err);
      }
    });

    req.on("error", reject);
  });
}

const server = http.createServer(async (req, res) => {
  try {
    if (req.method === "POST" && req.url === "/init") {
      await readJson(req);
      initDone = true;
      sendJson(res, 200, { ok: true });
      return;
    }

    if (req.method === "POST" && req.url === "/run") {
      const payload = await readJson(req);
      const params = payload.value || payload;
      const result = await runAction(params);
      sendJson(res, 200, result);
      return;
    }

    if (req.method === "GET" && req.url === "/health") {
      sendJson(res, 200, { status: "ok", init_done: initDone });
      return;
    }

    sendJson(res, 404, { error: "not found" });
  } catch (err) {
    sendJson(res, 502, {
      error: err.message,
      stack: err.stack
    });
  }
});

server.listen(8080, "0.0.0.0", () => {
  console.log("Listening on 0.0.0.0:8080");
});