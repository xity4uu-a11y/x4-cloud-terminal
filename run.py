"""CLI entrypoint for X4 Cloud Terminal."""

from __future__ import annotations

import argparse
import logging
import uvicorn

from x4_cloud.config import CloudSettings
from x4_cloud.server import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="X4 Cloud Terminal — Cross-Platform Trading Hub & MT5 Cloud Bridge")
    parser.add_argument("--host", type=str, default=None, help="Bind host (default from HOST env or 0.0.0.0)")
    parser.add_argument("--port", type=int, default=None, help="Bind port (default from PORT env or 8501)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )
    settings = CloudSettings.from_env()
    host = args.host or settings.host
    port = args.port or settings.port

    app = create_app(settings=settings)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
