FROM python:3.11-slim

WORKDIR /action

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    gcc \
    libc6-dev \
    && rm -rf /var/lib/apt/lists/*

COPY app.py .
COPY ksm_wrap.c .
COPY watermark.png .
COPY hi_chitanda_eru.mp4 .

RUN gcc -O2 -o /usr/local/bin/ksm_wrap /action/ksm_wrap.c

EXPOSE 8080

ENTRYPOINT ["/usr/local/bin/ksm_wrap"]
CMD ["python", "app.py"]