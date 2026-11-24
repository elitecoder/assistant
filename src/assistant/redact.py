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
import sys
import threading
import traceback

PLACEHOLDER = "<redacted>"
# What stands in for text we couldn't clean: never the original.
WITHHELD = "<log text withheld: redaction failed>"

# Rule patterns are spelled the same in slack-reactor/src/redact.js; a test compares
# them, and runs one shared list of cases through both.
_NA = r"(?<![A-Za-z0-9])"
_VAL = r"[^\s&\"',;\\<]"
# Something that looks like a credential, not a word: 16+ characters holding a digit, or a known prefix.
_CRED = (r"(?:(?=[A-Za-z0-9._~+/=-]{0,512}[0-9])[A-Za-z0-9._~+/=-]{16,512}"
         r"|(?:xox[a-z]-|xapp-|gh[pousr]_|github_pat_|ya29\.|eyJ|sk-|AKIA|ASIA)[A-Za-z0-9._~+/=%-]{4,512})")
# After a header or field name: an optional (escaped) quote, the separator, an optional array bracket and quote.
_HEADER = r"[ \t]{0,8}[:=][ \t]{0,8}(?:\[[ \t]{0,8})?\\?[\"']?)"
_NAMES = r"(?:(?:access_|refresh_|id_)?token|client_secret|password|passwd|api[_-]?key)"

# (pattern, flags, replacement). \1 and \2 keep the text that says what the value was.
_SPECS = (
    # Headers.
    (r"(authorization\\?[\"']?" + _HEADER + r"(?:(?:bearer|basic|token|digest)[ \t]{1,8}[^\s\"',;\\\]]{1,512}|" + _CRED + ")",
     re.IGNORECASE, r"\1<redacted>"),
    (r"\b(bearer[ \t]{1,8})" + _CRED, re.IGNORECASE, r"\1<redacted>"),
    (r"\b((?:set-)?cookie\\?[\"']?" + _HEADER + r"[^\r\n\"'\\\]]{1,1024}", re.IGNORECASE, r"\1<redacted>"),
    # https://user:password@host keeps the host.
    (r"(\b[a-z][a-z0-9+.-]{1,20}://)[^\s/:@\"']{1,256}:[^\s/@\"']{1,256}@", re.IGNORECASE, r"\1<redacted>@"),
    # Slack incoming webhook: the last part is the secret.
    (r"(hooks\.slack\.com/services/[A-Za-z0-9]{1,24}/[A-Za-z0-9]{1,24}/)[A-Za-z0-9]{8,64}", 0, r"\1<redacted>"),
    # Named fields in query strings, form bodies, JSON (also escaped JSON) and Node's object dumps.
    # A quoted value runs to its closing quote, spaces and all.
    (_NA + "(" + _NAMES + r")(\\?[\"']?[ \t]{0,8}[=:][ \t]{0,8}\\?[\"'])[^\"'\\\r\n]{1,512}", re.IGNORECASE, r"\1\2<redacted>"),
    # Unquoted. A bare `token` keeps short values (token=12345 in a model log line is a count).
    (_NA + r"((?:access_|refresh_)token|client_secret|password|passwd|api[_-]?key)(\\?[\"']?[ \t]{0,8}=[ \t]{0,8})" + _VAL + "{1,512}",
     re.IGNORECASE, r"\1\2<redacted>"),
    (_NA + r"((?:id_)?token)(\\?[\"']?[ \t]{0,8}=[ \t]{0,8})" + _VAL + "{8,512}", re.IGNORECASE, r"\1\2<redacted>"),
    (_NA + r"(client_secret|api[_-]?key)(\\?[\"']?[ \t]{0,8}:[ \t]{0,8})" + _VAL + "{1,512}", re.IGNORECASE, r"\1\2<redacted>"),
    # Environment dumps: SOME_TOKEN=value.
    (r"(?<![A-Za-z0-9_])([A-Z][A-Z0-9_]{0,60}_(?:TOKEN|SECRET|PASSWORD|API_KEY)=)[^\s<\"'][^\s<]{0,511}", 0, r"\1<redacted>"),
    # Token shapes, keeping the prefix.
    (_NA + r"(xox[a-z](?:-|%2[dD])|xapp(?:-|%2[dD]))(?:[A-Za-z0-9-]|%[0-9A-Fa-f]{2}){6,300}", 0, r"\1<redacted>"),
    (_NA + r"(gh[pousr]_)[A-Za-z0-9]{20,255}", 0, r"\1<redacted>"),
    (_NA + r"(github_pat_)[A-Za-z0-9_]{20,255}", 0, r"\1<redacted>"),
    (_NA + r"(ya29\.)[A-Za-z0-9_.-]{20,2048}", 0, r"\1<redacted>"),
    (r"(?<![A-Za-z0-9/:.])(1//)[A-Za-z0-9_-]{20,512}", 0, r"\1<redacted>"),
    (_NA + r"(AKIA|ASIA)[A-Z0-9]{16}(?![A-Za-z0-9])", 0, r"\1<redacted>"),
    (_NA + r"(GOCSPX-)[A-Za-z0-9_-]{16,64}", 0, r"\1<redacted>"),
    # Model keys: not after a letter, digit, hyphen, slash, dot or underscore (task-sk-..., a path), and a real key holds a digit.
    (r"(?<![A-Za-z0-9/._-])(sk-ant-)[A-Za-z0-9_-]{16,300}", 0, r"\1<redacted>"),
    (r"(?<![A-Za-z0-9/._-])(sk-)(?=[A-Za-z0-9_-]{0,300}[0-9])[A-Za-z0-9_-]{32,300}", 0, r"\1<redacted>"),
    # A JWT on its own (Microsoft Graph access tokens are JWTs). The lookbehind keeps a long run
    # from being rescanned from every eyJ inside it.
    (r"(?<![A-Za-z0-9_-])(eyJ)[A-Za-z0-9_-]{8,2048}\.[A-Za-z0-9_-]{4,4096}\.[A-Za-z0-9_-]{4,2048}", 0, r"\1<redacted>"),
)
_RULES = tuple((re.compile(pattern, flags), replacement) for pattern, flags, replacement in _SPECS)


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
            try:
                text = record.getMessage()
            except Exception:  # noqa: BLE001 - a bad format call still logs its line
                text = str(record.msg)
            record.msg = redact(text)
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


def _print_crash(exc_type, exc, tb) -> None:
    try:
        text = redact("".join(traceback.format_exception(exc_type, exc, tb)))
    except Exception:  # noqa: BLE001
        text = WITHHELD + "\n"
    sys.stderr.write(text)


def _excepthook(exc_type, exc, tb) -> None:
    _print_crash(exc_type, exc, tb)


def _thread_excepthook(args) -> None:
    if args.exc_type is SystemExit:
        return
    sys.stderr.write(f"Exception in thread {getattr(args.thread, 'name', '?')}:\n")
    _print_crash(args.exc_type, args.exc_value, args.exc_traceback)


def install(logger: logging.Logger | None = None) -> logging.Logger:
    """Put the filter on `logger` (the root logger by default) and on each of its
    handlers. The handlers matter: a logger's own filters skip records that come
    up from child loggers. Also makes an uncaught exception print its traceback
    through `redact`. Safe to call again; call it after the handlers exist."""
    logger = logger if logger is not None else logging.getLogger()
    _attach(logger)
    for handler in logger.handlers:
        _attach(handler)
    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook
    return logger
