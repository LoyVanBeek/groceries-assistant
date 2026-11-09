"""CLI entry point: `groceries-assistant serve` starts the web app."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(help="Weekly dinner planner + AH shopping list builder")

LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "()": "uvicorn.logging.DefaultFormatter",
            "fmt": "%(asctime)s %(levelprefix)s %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
        "access": {
            "()": "uvicorn.logging.AccessFormatter",
            "fmt": '%(asctime)s %(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
    },
    "handlers": {
        "default": {
            "class": "logging.StreamHandler",
            "formatter": "default",
            "stream": "ext://sys.stderr",
        },
        "access": {
            "class": "logging.StreamHandler",
            "formatter": "access",
            "stream": "ext://sys.stdout",
        },
    },
    "loggers": {
        "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
        "uvicorn.error": {"level": "INFO"},
        "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
    },
}


def default_data_dir() -> Path:
    return Path(os.environ.get("GROCERIES_DATA_DIR", "data"))


@app.callback()
def _root() -> None:
    """Weekly dinner planner + AH shopping list builder."""


@app.command()
def serve(
    host: str = "0.0.0.0",
    port: int = 8901,
    data_dir: Annotated[
        Path | None, typer.Option(help="Data directory (default $GROCERIES_DATA_DIR).")
    ] = None,
) -> None:
    """Run the web UI."""
    import uvicorn

    from groceries_assistant.web.app import create_app

    app_instance = create_app(
        Path(data_dir) if data_dir else default_data_dir(),
        tesseract_binary=os.environ.get("GROCERIES_TESSERACT", "tesseract"),
    )
    uvicorn.run(app_instance, host=host, port=port, log_config=LOGGING_CONFIG)


if __name__ == "__main__":
    app()
