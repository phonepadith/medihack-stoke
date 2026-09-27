FROM python:3.11-slim

# ffmpeg normalizes variable-frame-rate WebM from the browser recorder;
# libglib2.0-0 is the one system lib opencv-headless still links against.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY vitals/ ./vitals/
COPY static/ ./static/
COPY mtts_can.hdf5 .

# Accounts and the session signing key live on a volume, not in the image
# layer: autostart.sh recreates this container whenever the image is rebuilt or
# a stale one will not start, and every clinician would otherwise have to
# register again after each of those.
RUN mkdir -p /data
VOLUME /data

ENV PORT=8080 \
    MTTS_CAN_WEIGHTS=/app/mtts_can.hdf5 \
    VITALS_DB=/data/vitals.db \
    TF_CPP_MIN_LOG_LEVEL=3

EXPOSE 8080
CMD ["python", "app.py"]
