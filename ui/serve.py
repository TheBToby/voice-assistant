#!/usr/bin/env python3
"""Console launcher: runs the FastAPI app with uvicorn, configured via env."""

from __future__ import annotations

import os
import sys

APP_DIR = os.path.dirname(os.path.abspath(__file__)) + "/app"


def main() -> None:
    sys.path.insert(0, APP_DIR)
    import uvicorn

    uvicorn.run(
        "main:app",
        host=os.getenv("UI_HOST", "0.0.0.0"),
        port=int(os.getenv("UI_PORT", "8090")),
        root_path=os.getenv("UI_ROOT_PATH", ""),
        log_level=os.getenv("UI_LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    main()
