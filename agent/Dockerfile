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
    && chown -R agent:agent /home/agent /app
USER agent

WORKDIR /app/agent
# "start" = production mode; agents auto-dispatch into any new room.
CMD ["python", "main.py", "start"]
