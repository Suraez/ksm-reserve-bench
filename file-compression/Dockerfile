
FROM python:3.11-slim

WORKDIR /action

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libc6-dev \
    && rm -rf /var/lib/apt/lists/*

COPY app.py .
COPY ksm_wrap.c .
COPY acmart-master /action/acmart-master

RUN gcc -O2 -o /usr/local/bin/ksm_wrap /action/ksm_wrap.c

ENV COMPRESSION_ENABLE_HTTP=0

EXPOSE 8080

ENTRYPOINT ["/usr/local/bin/ksm_wrap"]
CMD ["python", "app.py"]
