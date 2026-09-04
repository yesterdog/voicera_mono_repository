"""
Minimal structured logging setup for the Asterisk bridge.

Independent, minimal implementation (not vendored from AVA-AI-Voice-Agent-
for-Asterisk — see NOTICE) providing just the `configure_logging()` /
`get_logger()` interface that ari_client.py and rtp_server.py expect: JSON
logs to stdout via structlog. Deliberately skips AVA's own correlation-ID
context vars, file rotation, and redaction rules — none of which this
bridge needs.
"""

import logging
import sys

import structlog


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(level) if isinstance(level, str) else level
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str):
    return structlog.get_logger(name)
