"""Keep secrets out of logs.

`redact(text)` swaps token-shaped strings for `<redacted>`, keeping the few
leading characters that say what kind of token it was (`xoxb-<redacted>`).
`install()` puts the same cleaning on a logger's handlers, so a service that
logs through `logging` can't write a token to its log file.

work_queue/scrubber.py cleans text on its way to a model and needs Python 3.11
(possessive quantifiers). This module has to load on the system Python (3.9) that
some launchd services run on, so every pattern is a plain, bounded, linear
regex: no possessive quantifiers, no atomic groups, no unbounded repeats.
"""
from __future__ import annotations

import logging
import re

PLACEHOLDER = "<redacted>"
# What stands in for text we couldn't clean: never the original.
WITHHELD = "<log text withheld: redaction failed>"

_NOT_ALNUM = r"(?<![A-Za-z0-9])"
_VALUE = r"[^\s&\"',;\\]{1,512}"

# (pattern, replacement). Order matters only in that the header patterns run
# before the bare-token ones, so "Authorization: Bearer xoxb-..." is one match.
_RULES = tuple((re.compile(pattern, flags), replacement) for pattern, flags, replacement in (
    # Header values: the whole value, with or without a scheme word.
    (r"(authorization[\"']?[ \t]{0,8}[:=][ \t]{0,8}[\"']?)(?:(?:bearer|basic|token)[ \t]{1,8})?" + _VALUE,
     re.IGNORECASE, r"\1" + PLACEHOLDER),
    (r"\b(bearer[ \t]{1,8})[A-Za-z0-9._~+/=-]{8,512}", re.IGNORECASE, r"\1" + PLACEHOLDER),
    # Slack.
    (_NOT_ALNUM + r"(xox[a-z]-|xapp-)[A-Za-z0-9-]{6,300}", 0, r"\1" + PLACEHOLDER),
    # GitHub.
    (_NOT_ALNUM + r"(gh[pousr]_)[A-Za-z0-9]{20,255}", 0, r"\1" + PLACEHOLDER),
    (_NOT_ALNUM + r"(github_pat_)[A-Za-z0-9_]{20,255}", 0, r"\1" + PLACEHOLDER),
    # Google: access tokens and refresh tokens.
    (_NOT_ALNUM + r"(ya29\.)[A-Za-z0-9_.-]{20,2048}", 0, r"\1" + PLACEHOLDER),
    (r"(?<![A-Za-z0-9/:.])(1//)[A-Za-z0-9_-]{20,512}", 0, r"\1" + PLACEHOLDER),
    # AWS access key ids.
    (_NOT_ALNUM + r"(AKIA|ASIA)[A-Z0-9]{16}(?![A-Za-z0-9])", 0, r"\1" + PLACEHOLDER),
    # Model providers. Anthropic first: its keys also match the generic sk- shape.
    (_NOT_ALNUM + r"(sk-ant-)[A-Za-z0-9_-]{16,300}", 0, r"\1" + PLACEHOLDER),
    (_NOT_ALNUM + r"(sk-)[A-Za-z0-9_-]{32,300}", 0, r"\1" + PLACEHOLDER),
    # Named fields in query strings, form bodies, JSON and Node's object dumps:
    # token=..., "access_token": "...", token: '...'. A bare "token: word" in prose stays.
    (_NOT_ALNUM + r"((?:access_|refresh_|id_)?token|client_secret)"
     r"([\"']?[ \t]{0,8}=[ \t]{0,8}[\"']?|[\"']?[ \t]{0,8}:[ \t]{0,8}[\"'])" + _VALUE,
     0, r"\1\2" + PLACEHOLDER),
))


def redact(text: str) -> str:
    """`text` with secrets replaced. Never raises; on any failure returns WITHHELD."""
    try:
        if not isinstance(text, str):
            text = str(text)
        for pattern, replacement in _RULES:
            text = pattern.sub(replacement, text)
        return text
    except Exception:  # noqa: BLE001 - the contract is "never raise, never leak"
        return WITHHELD


class RedactingFilter(logging.Filter):
    """Cleans a record's fully formatted message, traceback and stack text."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact(record.getMessage())
            record.args = None
            if record.exc_info and not record.exc_text:
                record.exc_text = logging.Formatter().formatException(record.exc_info)
            if record.exc_text:
                record.exc_text = redact(record.exc_text)
            if record.stack_info:
                record.stack_info = redact(record.stack_info)
        except Exception:  # noqa: BLE001 - a logging call must not take the service down
            record.msg, record.args = WITHHELD, None
            record.exc_info, record.exc_text, record.stack_info = None, None, None
        return True


def _attach(target) -> None:
    if not any(isinstance(f, RedactingFilter) for f in target.filters):
        target.addFilter(RedactingFilter())


def install(logger: logging.Logger | None = None) -> logging.Logger:
    """Put the filter on `logger` (the root logger by default) and on each of its
    handlers. The handlers matter: a logger's own filters skip records that come
    up from child loggers. Safe to call again; call it after the handlers exist."""
    logger = logger if logger is not None else logging.getLogger()
    _attach(logger)
    for handler in logger.handlers:
        _attach(handler)
    return logger
