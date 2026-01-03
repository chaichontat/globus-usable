from __future__ import annotations

import os
import sys
import logging

_configured = False

logger = logging.getLogger("globus_usable")


def configure_logger() -> None:
    global _configured
    if _configured:
        return
    _configured = True

    level = os.environ.get("GLOBUS_USABLE_LOG_LEVEL", "INFO")
    logger.setLevel(level)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
