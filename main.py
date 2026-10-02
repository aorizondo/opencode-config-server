"""Entrypoint: `python main.py`, or `uvicorn main:app`."""

from __future__ import annotations

import logging
import os

from opencode_config_server.app import create_app

logging.basicConfig(
    level=os.getenv("OCS_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.getenv("OCS_HOST", "0.0.0.0"), port=int(os.getenv("OCS_PORT", "8000")))
