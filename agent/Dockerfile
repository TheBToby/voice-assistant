FROM python:3.12-slim

# ONNX Runtime (ships with the silero VAD / turn-detector plugins) is built with
# Microsoft telemetry. Without this opt-out it persists a telemetry device ID
# under ~/.cache/Microsoft/DeveloperTools/.onnxruntime and warns
#   "Failed to persist telemetry device ID; using an in-memory identifier"
# whenever ~/.cache (= the model-cache volume mount) is not owned by the
# container user - e.g. root-owned MODEL_CACHE_DIR bind mounts, stale root-owned
# volumes, or rootless-Docker uid mapping. This env var latches a full opt-out
# before ORT initializes: no device ID, no telemetry uploader, no warning.
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/home/agent \
    ORT_DISABLE_TELEMETRY=1

WORKDIR /app

# system deps: none required beyond runtime (silero/turn-detector ship ONNX
# runtime wheels; model files are downloaded to $HOME/.cache at first start)
COPY agent/requirements.txt /app/agent/requirements.txt
RUN pip install --no-cache-dir -r /app/agent/requirements.txt

# application code + tooling (token gen / smoke test run in this image too)
COPY agent/ /app/agent/
COPY scripts/ /app/scripts/
COPY tests/ /app/tests/

RUN useradd --create-home --shell /bin/bash agent \
    && chown -R agent:agent /home/agent /app \
    # /home/agent/.cache is a Docker volume mount point (model-cache). It must
    # exist with agent ownership in the image so a FRESH named volume inherits
    # it - otherwise the volume is created root-owned and model downloads fail.
    && mkdir -p /home/agent/.cache/huggingface \
    && chown -R agent:agent /home/agent/.cache
USER agent

WORKDIR /app/agent
# "start" = production mode; agents auto-dispatch into any new room.
CMD ["python", "main.py", "start"]
