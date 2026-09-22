"""Reads documents from a JSON file. Used for tests and for developing the server without
touching any external site. Fixture text is deliberately marked so it can never be mistaken
for real statutory language."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Iterator

from ..models import ParsedDoc, ParsedPublicAct


class FixtureSource:
    def __init__(self, path: str | Path, name: str | None = None):
        p = Path(path)
        if p.is_dir():
            p = p / "data.json"
        raw = json.loads(p.read_text(encoding="utf-8"))
        self._docs = raw.get("documents", [])
        self._pas = raw.get("public_acts", [])
        self.name = name or raw.get("source", "mcl")
        self.complete = bool(raw.get("complete", True))

    def iter_documents(self) -> Iterator[ParsedDoc]:
        for d in self._docs:
            yield ParsedDoc(source=self.name, **d)

    def iter_public_acts(self) -> Iterable[ParsedPublicAct]:
        for pa in self._pas:
            yield ParsedPublicAct(**pa)
