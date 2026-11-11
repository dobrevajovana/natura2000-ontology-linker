"""Expert decisions on predicted mappings, kept as an append-only JSON-lines file.

Every decision is kept, with who made it and when; nothing is rewritten. The
current state of a mapping is each reviewer's latest decision; when reviewers'
latest decisions differ, the mapping is "disputed". Used by the MFD and cloud
viewers and by the generic review tool alike.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

VERDICTS = ("correct", "incorrect", "unsure")
MATCH_TYPES = ("skos:exactMatch", "skos:closeMatch", "skos:broadMatch", "skos:narrowMatch", "skos:relatedMatch")
MAX_REVIEWER, MAX_COMMENT = 80, 2000


class ReviewError(ValueError):
    """A review that cannot be stored; the message is safe to show the reviewer."""


class ReviewStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def events(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue  # a half-written last line after a crash; the rest stands
        return out

    def add(self, body: dict, predicted: dict[str, dict]) -> dict:
        """Validate one decision against the predicted mappings (by key) and append it."""
        key = str(body.get("key") or "")
        if key not in predicted:
            raise ReviewError("Unknown match; reload the page.")
        verdict = body.get("verdict")
        if verdict not in VERDICTS:
            raise ReviewError(f"Verdict must be one of: {', '.join(VERDICTS)}.")
        reviewer = " ".join(str(body.get("reviewer") or "").split())
        if not reviewer or len(reviewer) > MAX_REVIEWER:
            raise ReviewError(f"Enter your name (up to {MAX_REVIEWER} characters) before reviewing.")
        comment = str(body.get("comment") or "").strip()
        if len(comment) > MAX_COMMENT:
            raise ReviewError(f"Comment is longer than {MAX_COMMENT} characters.")
        match_type = body.get("match_type") or None
        if match_type is not None and match_type not in MATCH_TYPES:
            raise ReviewError("Unknown match type.")
        if verdict != "correct":
            match_type = None  # a corrected type only means something for a correct link
        event = {
            "key": key, "verdict": verdict, "match_type": match_type, "comment": comment, "reviewer": reviewer,
            "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "predicted": predicted[key],
        }
        line = json.dumps(event, ensure_ascii=False) + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
        return event

    def state(self) -> dict[str, dict]:
        """Per mapping: latest decision per reviewer, overall latest, and whether reviewers disagree."""
        out: dict[str, dict] = {}
        for ev in self.events():
            s = out.setdefault(ev["key"], {"reviewers": {}, "history": []})
            s["reviewers"][ev["reviewer"]] = ev
            s["history"].append({k: ev.get(k) for k in ("verdict", "match_type", "comment", "reviewer", "time")})
        for s in out.values():
            latest = max(s["reviewers"].values(), key=lambda ev: ev["time"])
            opinions = {(ev["verdict"], ev["match_type"]) for ev in s["reviewers"].values()}
            s.update(verdict=latest["verdict"], match_type=latest["match_type"], comment=latest["comment"],
                     reviewer=latest["reviewer"], time=latest["time"], disputed=len(opinions) > 1)
            s["reviewers"] = {r: {"verdict": ev["verdict"], "match_type": ev["match_type"], "time": ev["time"]}
                              for r, ev in s["reviewers"].items()}
        return out
