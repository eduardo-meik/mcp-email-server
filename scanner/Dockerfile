# ClamAV scanner for Cloud Run (2 GiB, min-instances 0).
# Signatures are baked into the image at build time; a nightly Cloud Build
# rebuild keeps them fresh (cold starts never run freshclam).
FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        clamav-daemon \
        clamav-freshclam \
        ca-certificates \
        netcat-openbsd \
        python3 \
        python3-pip \
        python3-venv \
    && rm -rf /var/lib/apt/lists/*

# Bake the signature database into the image.
RUN freshclam --verbose \
    && (test -s /var/lib/clamav/main.cvd || test -s /var/lib/clamav/main.cld)

COPY clamd.conf /etc/clamav/clamd.conf

RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY app.py /app/app.py
WORKDIR /app

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8080
ENTRYPOINT ["/entrypoint.sh"]
