"""Internal metadata-driven extraction with original Python character offsets.

Not a public API contract. Unknown text remains verbatim; suggested corrections
never change the query. No provider, filesystem or database access at inference.
"""
from __future__ import annotations
from dataclasses import asdict, dataclass
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any
from ..agent.domain_pack import LoadedDomainPack

VERSION = "conservative-semantics.internal.v1"


def normalized_with_offsets(text: str):
    chars, offsets = [], []
    for pos, char in enumerate(text):
        for piece in unicodedata.normalize("NFKC", char).casefold():
            if piece.isspace():
                if not chars or chars[-1] == " ":
                    if offsets and chars[-1] == " ":
                        offsets[-1] = (offsets[-1][0], pos + 1)
                    continue
                piece = " "
            chars.append(piece)
            offsets.append((pos, pos + 1))
    return "".join(chars), offsets


@dataclass(frozen=True)
class Mention:
    start: int
    end: int
    text: str
    concepts: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    status: str


class ConservativeSemantics:
    def __init__(self, pack: LoadedDomainPack):
        if not isinstance(pack, LoadedDomainPack):
            raise TypeError("validated domain pack required")
        self.pack = pack
        self.names = {str(x["concept_id"]): str(x["display_name"])
                      for x in pack.artifact_items("concepts")}
        self.aliases = [(str(x["surface_form"]), str(x["concept_id"]), str(x["alias_id"]))
                        for x in pack.artifact_items("aliases")]
        self.aliases += [(name, key, key) for key, name in self.names.items()]

    def analyze(self, query: str) -> dict[str, Any]:
        if not isinstance(query, str) or not query.strip() or len(query) > 8192:
            raise ValueError("invalid semantics query")
        normalized, offsets = normalized_with_offsets(query)
        hits: dict[tuple[int, int], list[tuple[str, str]]] = {}
        for surface, concept, ref in self.aliases:
            needle = normalized_with_offsets(surface)[0].strip()
            if not needle:
                continue
            pattern = re.escape(needle)
            if needle.isascii() and needle.replace(" ", "").isalnum():
                pattern = r"(?<![a-z0-9_])" + pattern + r"(?![a-z0-9_])"
            for match in re.finditer(pattern, normalized):
                start, end = offsets[match.start()][0], offsets[match.end()-1][1]
                hits.setdefault((start, end), []).append((concept, ref))
        # Strictly nested aliases are owned by the longer mention. Crossing
        # matches remain ambiguous and cannot authorize a rewrite.
        spans = [s for s in hits if not any(t != s and t[0] <= s[0] and s[1] <= t[1] for t in hits)]
        mentions, edits = [], []
        protected = [m.span() for m in re.finditer(
            r"(?:不要|不含|不包括|不超过|不少于|非|无|未|不是|不得|排除|\bnot\b|\bwithout\b|\bexcept\b)|[+-]?\d+(?:[.,]\d+)*(?:\s*(?:万元|亿元|元|台|件|个|张|首|秒|分钟|小时|天|年|月|kg|g|km|m|%|％))?",
            query, re.IGNORECASE)]
        for start, end in sorted(spans):
            entries = hits[(start, end)]
            concepts = tuple(sorted({x[0] for x in entries}))
            overlap = any((a, b) != (start, end) and a < end and start < b for a, b in spans)
            status = "ambiguous" if len(concepts) != 1 or overlap else "resolved"
            mention = Mention(start, end, query[start:end], concepts,
                              tuple(sorted({x[1] for x in entries})), status)
            mentions.append(mention)
            if status == "resolved":
                replacement = self.names[concepts[0]]
                if replacement != mention.text and not any(a < end and start < b for a, b in protected):
                    edits.append({"start": start, "end": end, "before": mention.text,
                                  "after": replacement, "kind": "alias_normalization",
                                  "evidence_refs": list(mention.evidence_refs)})
        rewritten = query
        for edit in reversed(edits):
            rewritten = rewritten[:edit["start"]] + edit["after"] + rewritten[edit["end"]:]
        # Gaps retain negation, numeric units, unknown types and all business
        # constraints. They are not guessed to be a particular entity type.
        gaps, cursor = [], 0
        for start, end in sorted(spans):
            if cursor < start:
                gaps.append({"start": cursor, "end": start, "text": query[cursor:start], "status": "uninterpreted"})
            cursor = max(cursor, end)
        if cursor < len(query):
            gaps.append({"start": cursor, "end": len(query), "text": query[cursor:], "status": "uninterpreted"})
        suggestions = []
        for token in re.finditer(r"[A-Za-z]{3,}", query):
            if any(a <= token.start() and token.end() <= b for a, b in spans):
                continue
            candidates = sorted({concept for surface, concept, _ in self.aliases
                                 if surface.isascii() and SequenceMatcher(None, token.group().casefold(), surface.casefold()).ratio() >= .8})
            if candidates:
                suggestions.append({"start": token.start(), "end": token.end(), "text": token.group(),
                                    "concepts": candidates, "status": "unconfirmed_spelling"})
        return {"implementation_version": VERSION, "path": "rules", "model_calls": 0,
                "domain_pack_id": self.pack.domain_pack_id, "domain_pack_version": self.pack.version,
                "domain_pack_sha256": self.pack.manifest_sha256,
                "original_query": query, "rewritten_query": rewritten,
                "mentions": [asdict(x) for x in mentions], "changes": edits,
                "uninterpreted": gaps, "suggestions": suggestions,
                "requires_clarification": not mentions or any(x.status == "ambiguous" for x in mentions) or bool(suggestions),
                "sql_execution_authorized": False}
