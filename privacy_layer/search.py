"""Local BM25 search over past incident postmortems (no model download, stdlib only).

    hits = search_incidents("checkout 500s after deploy, connection pool exhausted", k=3)
    # [{"id": "INC-2025-014", "title": ..., "score": 9.81, "root_cause": ..., "fix": ...}, ...]
"""
import math
import re
from collections import Counter
from pathlib import Path

DEFAULT_DIR = Path(__file__).resolve().parents[1] / "data" / "past_incidents"
K1, B = 1.5, 0.75
TITLE_BOOST = 2  # title tokens counted this many extra times

_STOP = set("""a an and are as at be been but by for from has have in into is it its of on or
that the their then there these this to was were which while will with after before our we
not no all any can over under up down out off very""".split())


def _stem(t):
    for suf in ("ing", "ed", "es", "s"):
        if len(t) > len(suf) + 3 and t.endswith(suf):
            return t[: -len(suf)]
    return t


def tokenize(text):
    return [_stem(t) for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in _STOP and len(t) > 1]


def _parse(path):
    text = path.read_text(encoding="utf-8")
    title_line = text.splitlines()[0].lstrip("# ").strip() if text else path.stem
    iid, _, title = title_line.partition(":")
    sections = {m.group(1).strip().lower(): m.group(2).strip()
                for m in re.finditer(r"^## (.+?)\n(.*?)(?=^## |\Z)", text, re.S | re.M)}
    return {"id": iid.strip() or path.stem, "title": title.strip() or title_line, "path": str(path),
            "symptoms": sections.get("symptoms", ""), "root_cause": sections.get("root cause", ""),
            "fix": sections.get("fix", ""), "_text": text}


class BM25Index:
    def __init__(self, docs):
        self.docs = docs
        self.tfs, self.lens = [], []
        df = Counter()
        for d in docs:
            toks = tokenize(d["_text"]) + tokenize(d["title"]) * TITLE_BOOST
            tf = Counter(toks)
            self.tfs.append(tf)
            self.lens.append(len(toks))
            df.update(tf.keys())
        n = len(docs)
        self.avgdl = sum(self.lens) / n if n else 0.0
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def search(self, query, k=3):
        q = tokenize(query)
        scored = []
        for i, tf in enumerate(self.tfs):
            s = 0.0
            for t in q:
                f = tf.get(t)
                if f:
                    s += self.idf[t] * f * (K1 + 1) / (f + K1 * (1 - B + B * self.lens[i] / self.avgdl))
            if s > 0:
                scored.append((s, i))
        scored.sort(reverse=True)
        out = []
        for s, i in scored[:k]:
            d = {key: v for key, v in self.docs[i].items() if not key.startswith("_")}
            d["score"] = round(s, 3)
            out.append(d)
        return out


_cache = {}


def _index(incidents_dir):
    d = Path(incidents_dir or DEFAULT_DIR)
    files = sorted(d.glob("*.md"))
    key = (str(d), tuple((f.name, f.stat().st_mtime_ns) for f in files))
    if key not in _cache:
        _cache.clear()
        _cache[key] = BM25Index([_parse(f) for f in files])
    return _cache[key]


def search_incidents(query, k=3, incidents_dir=None):
    """Top-k past incidents for query, best first. Each hit has id, title, score (BM25),
    symptoms, root_cause, fix, path. Returns [] if nothing matches."""
    return _index(incidents_dir).search(query, k)


if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:]) or "checkout failing after deploy, database connection pool exhausted, timeouts"
    for h in search_incidents(q):
        print(f"{h['score']:>7.3f}  {h['id']}  {h['title']}")
