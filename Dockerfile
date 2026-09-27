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

ENV PORT=8080 \
    MTTS_CAN_WEIGHTS=/app/mtts_can.hdf5 \
    TF_CPP_MIN_LOG_LEVEL=3

EXPOSE 8080
CMD ["python", "app.py"]
