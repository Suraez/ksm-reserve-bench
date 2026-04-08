FROM python:3.11-slim

WORKDIR /action

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libc6-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY ksm_wrap.c .
COPY gene1.txt .
COPY gene2.txt .

RUN gcc -O2 -o /usr/local/bin/ksm_wrap /action/ksm_wrap.c

EXPOSE 8080

ENTRYPOINT ["/usr/local/bin/ksm_wrap"]
CMD ["python", "app.py"]