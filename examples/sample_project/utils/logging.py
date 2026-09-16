"""Logging factory used across all modules."""

import logging

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_configured = {}


def get_logger(name, level=logging.INFO):
    """Return a configured logger for ``name``.

    Loggers are created once and reused; handlers are attached only on
    first use so repeated calls never duplicate output.
    """
    if name in _configured:
        return _configured[name]
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(_FORMAT))
        logger.addHandler(handler)
    logger.setLevel(level)
    _configured[name] = logger
    return logger
