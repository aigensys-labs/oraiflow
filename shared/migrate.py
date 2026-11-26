"""One-shot additive schema and role bootstrap for Docker Compose."""

from __future__ import annotations

import logging
import os

from shared.database import ensure_runtime_schema


def main() -> None:
    """Apply the idempotent database schema and synthetic-data migrations."""
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    logging.getLogger("oraiflow.migrate").info(
        "Applying additive local schema and MCP read-role migrations"
    )
    ensure_runtime_schema()
    logging.getLogger("oraiflow.migrate").info("Database migrations completed successfully")


if __name__ == "__main__":
    main()
