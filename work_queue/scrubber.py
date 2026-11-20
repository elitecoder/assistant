"""Remove what looks like a secret from text before it reaches a model.

A pattern match: tokens and keys by their known shapes, credentials in links and
headers, "password ... is X" phrases, one-time codes, and long runs of letters
and digits. It can miss an unusual format, and it also removes long runs such as
a full commit hash.
"""
from __future__ import annotations

import re

SECRET = re.compile(
    r"x(?:ox[a-z]|app)-[A-Za-z0-9-]{8,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|sk-[A-Za-z0-9_-]{16,}|[sr]k_(?:live|test)_[A-Za-z0-9]{16,}|AIza[0-9A-Za-z_-]{35}|(?:AKIA|ASIA)[0-9A-Z]{16}"
    r"|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}|hooks\.slack\.com/services/\S+"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)|(?<=://)[^/\s:@]+:[^@\s/]+@"
    r"|(?i:authorization)\s*:\s*\S+(?:[ \t]+[^\s,;]+)?|(?i:(?:set-)?cookie)\s*:[^\n]*|(?i:bearer)\s+[A-Za-z0-9._~+/-]{12,}=*"
    r"""|(?<=[?&])(?i:sig|signature|x-amz-signature|x-amz-credential|x-goog-signature|access_token|token|code|key)=[^&\s"'<>]+"""
    r"""|["']?\b(?i:(?:[a-z]+_)*secret(?:_[a-z]+)*|password|passwd|passcode|pin|otp|token|api[_-]?key"""
    r"""|(?:one-time|verification|security|login)\s+code)\b["']?(?:\s+[A-Za-z]+){0,3}?\s*"""
    r"""(?:=>|[:=]|\b(?i:is)\b(?:\s*(?:=>|[:=-]))?)\s*(?:"[^"\n]*"|'[^'\n]*'|["']?[^\s"',;]+["']?)"""
    r"|\b(?:G-)?\d{4,8}\s+(?i:is\s+your)\b[^\n.]{0,40}?\b(?i:code|pin|password)\b"
    r"|\b(?i:code)\b\s*(?:(?i:is)\s*)?[:=]?\s*(?:G-)?\d{4,8}\b"
    r"|\b[A-Fa-f0-9]{32,}\b|(?=[A-Za-z0-9+_-]*\d)(?=[A-Za-z0-9+_-]*[A-Za-z])\b[A-Za-z0-9+_-]{40,}={0,2}"
    r"|(?<![A-Za-z0-9/+])(?=[A-Za-z0-9/+]*\d)(?=[A-Za-z0-9/+]*[A-Z])(?=[A-Za-z0-9/+]*[a-z])(?=[A-Za-z0-9/+]*[/+])"
    r"[A-Za-z0-9/+]{40,}={0,2}", re.S)


def scrub(text: str) -> str:
    return SECRET.sub("[removed]", text or "")
