FROM node:18-bullseye

WORKDIR /action

RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    gcc \
    g++ \
    make \
    libc6-dev \
    python3 \
    && rm -rf /var/lib/apt/lists/*

COPY package.json .
RUN npm install

COPY app.js .
COPY ksm_wrap.c .
COPY src /action/src

RUN gcc -O2 -o /usr/local/bin/ksm_wrap /action/ksm_wrap.c

EXPOSE 8080

ENTRYPOINT ["/usr/local/bin/ksm_wrap"]
CMD ["node", "app.js"]