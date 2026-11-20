"""Remove what looks like a secret from text before it reaches a model.

A pattern match: tokens and keys by their known shapes, credentials and
signatures in links and headers, `key: value` pairs whose key names a secret,
"password ... is X" phrases, one-time codes, and long runs of letters and
digits. It can miss an unusual format. It also removes long hexadecimal runs,
such as a full commit hash, from what the model sees; the page always shows the
original text.

Every pattern runs in time linear in its input: runs are matched only from
where they start, repeats are possessive or bounded, and no quantifier nests
inside another. Text is also cut to SCRUB_MAX characters first.
"""
from __future__ import annotations

import re

SCRUB_MAX = 8000
SHAPES = re.compile(
    r"x(?:ox[a-z]|app)-[A-Za-z0-9-]{8,}+|gh[pousr]_[A-Za-z0-9]{20,}+|github_pat_[A-Za-z0-9_]{20,}+"
    r"|sk-[A-Za-z0-9_-]{16,}+|[sr]k_(?:live|test)_[A-Za-z0-9]{16,}+|AIza[0-9A-Za-z_-]{35}|(?:AKIA|ASIA)[0-9A-Z]{16}"
    r"|eyJ[A-Za-z0-9_-]{8,}+\.[A-Za-z0-9_-]{8,}+\.[A-Za-z0-9_-]{8,}+|hooks\.slack\.com/services/\S++"
    r"|-----BEGIN (?:[A-Z]{1,20} ){0,2}PRIVATE KEY-----.*?(?:-----END (?:[A-Z]{1,20} ){0,2}PRIVATE KEY-----|\Z)"
    r"|(?<=://)[^/\s:@]++:[^@\s/]++@"
    r"|(?i:authorization)[ \t]*+:[ \t]*+\S++(?:[ \t]++[^\s,;]++)?+|(?i:(?:set-)?cookie)[ \t]*+:[^\n]*+"
    r"|(?i:bearer)[ \t]++[A-Za-z0-9._~+/-]{12,}+=*+"
    r"""|(?<=[?&])(?i:sig|signature|x-amz-signature|x-amz-credential|x-goog-signature|access_token|token|code|key)"""
    r"""=[^&\s"'<>]++"""
    r"|(?<![A-Za-z0-9_])(?i:passphrase)[ \t]*+(?:(?i:is)\b[ \t]*+)?+[:=]?+[ \t]*+[^\s][^\n]*+"
    r"""|(?<![A-Za-z0-9_])(?i:password|passwd|passcode|(?:my|your|the)[ \t]++pass"""
    r"""|(?:one-time|verification|security|login)[ \t]++code)"""
    r"""(?:[ \t]++[A-Za-z]{1,20}+){0,3}?[ \t]*+(?:=>|[:=]|\b(?i:is)\b(?:[ \t]*+(?:=>|[:=-]))?+)[ \t]*+"""
    r"""(?:"[^"\n]{0,200}+"|'[^'\n]{0,200}+'|[^\s"',;]{1,200}+)"""
    r"|(?<![A-Za-z0-9_])(?i:password|passcode|passphrase)[ \t]*+:?+[ \t]*+\n[ \t]*+\S{1,200}+(?=[ \t]*+(?:\n|\Z))"
    r"|(?<![A-Za-z0-9])(?i:passcode|pin|otp)\b[ \t]*+(?:(?i:is)\b[ \t]*+)?+[:=]?+[ \t]*+(?:G-)?+\d{4,8}+\b"
    r"|(?<![A-Za-z0-9])(?i:code)\b[ \t]*+(?:(?i:is)\b[ \t]*+[:=]?+|[:=])[ \t]*+(?:G-)?+\d{4,8}+\b"
    r"|(?<![A-Za-z0-9-])(?:G-)?+\d{5,8}+[ \t]++(?i:is[ \t]++your)\b[^\n.]{0,40}?\b(?i:code|pin|password)\b"
    r"|(?<![A-Za-z0-9])(?i:use)[ \t]++\d{4,8}+[ \t]++(?i:to[ \t]++(?:sign|log)[ \t]*+in)\b"
    r"|(?<![A-Za-z0-9])[A-Fa-f0-9]{32,}+(?![A-Za-z0-9])"
    r"|(?<![A-Za-z0-9+_/.:-])(?=[A-Za-z+_-]*+[0-9])(?=[0-9+_-]*+[A-Za-z])"
    r"[A-Za-z0-9+_-]{40,}+={0,2}+(?![/])"
    r"|(?<![A-Za-z0-9/+.:])(?=[A-Za-z/+]*+[0-9])(?=[a-z0-9/+]*+[A-Z])(?=[A-Z0-9/+]*+[a-z])(?=[A-Za-z0-9]*+[/+])"
    r"[A-Za-z0-9/+]{40,}+={0,2}+", re.S)
KEYED = re.compile(r"""["']?+(?<![A-Za-z0-9_-])([A-Za-z][A-Za-z0-9_-]{0,40}+)["']?+[ \t]*+(?:=>|[:=])[ \t]*+"""
                   r"""(?:"[^"\n]{0,200}+"|'[^'\n]{0,200}+'|[^\s"',;]{1,200}+)""")
SECRET_NAME = re.compile(r"(?:^|[_-])(?:secret|pwd|passwd|password|token|apikey|api[_-]key|passcode)(?:$|[_-])", re.I)


def keyed(found: re.Match) -> str:
    return "[removed]" if SECRET_NAME.search(found.group(1)) else found.group(0)


def scrub(text: str) -> str:
    """The text, cut to SCRUB_MAX characters, with everything that looks like a secret replaced by [removed]."""
    return KEYED.sub(keyed, SHAPES.sub("[removed]", (text or "")[:SCRUB_MAX]))
