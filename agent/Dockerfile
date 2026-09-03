FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/home/agent

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
