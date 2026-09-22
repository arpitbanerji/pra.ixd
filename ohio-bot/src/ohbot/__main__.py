"""Entry point: ``python -m ohbot``."""

from __future__ import annotations

import asyncio

from ohbot.app import run
from ohbot.config import Settings
from ohbot.logging_setup import configure_logging


def main() -> None:
    settings = Settings()  # fails fast on missing or unsafe configuration
    configure_logging(settings.log_level, settings.log_json)
    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:  # pragma: no cover
        pass


if __name__ == "__main__":
    main()
