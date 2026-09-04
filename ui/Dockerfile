FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app/ui

COPY ui/requirements.txt /app/ui/requirements.txt
RUN pip install --no-cache-dir -r /app/ui/requirements.txt

COPY ui/ /app/ui/

# non-root like the agent; /data is the console-data volume mount point and
# must be writable - a fresh named volume inherits the ownership of the
# existing directory, so it is created here.
RUN useradd --create-home --shell /bin/bash console \
    && mkdir -p /data \
    && chown -R console:console /home/console /app/ui /data
USER console

EXPOSE 8090
CMD ["python", "/app/ui/serve.py"]
