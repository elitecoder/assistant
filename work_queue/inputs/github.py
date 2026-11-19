"""Open pull requests that ask for your review, read with the `gh` CLI's own sign-in."""
from __future__ import annotations

from datetime import datetime

from work_queue import tools

FIELDS = "number,title,url,repository,author,createdAt,isDraft"


def rows(entry: dict) -> list[dict]:
    prs = tools.run_json(["gh", "search", "prs", "--review-requested=@me", "--state=open", "--limit", "100",
                          "--json", FIELDS], env={"GH_HOST": entry["host"]} if entry.get("host") else None)
    skip = set(entry.get("skip_authors") or [])
    out = []
    for p in prs:
        author = (p.get("author") or {}).get("login", "")
        if author in skip:
            continue
        opened = datetime.fromisoformat(p["createdAt"].replace("Z", "+00:00")).strftime("%b %-d")
        out.append({"key": p["url"], "link": p["url"], "draft": bool(p.get("isDraft")),
                    "title": f"{p['repository']['nameWithOwner']}#{p['number']}: {p['title']}",
                    "meta": f"Review requested · {author} · opened {opened}"})
    return out
