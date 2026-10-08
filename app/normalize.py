"""CBV name normalization + duplicate detection.

Two layers:
  1. Confirmed merges  - decisions you made in the dashboard (stored locally).
  2. Suspected pairs   - fuzzy matches the dashboard asks you to confirm.
"""
from __future__ import annotations

import re
import unicodedata
from itertools import combinations

from rapidfuzz import fuzz

TITLES = {
    "mr", "mrs", "ms", "miss", "dr", "prof", "eng", "hon", "rev", "pastor",
    "chief", "mr.", "mrs.", "ms.", "dr.",
}

STOPWORDS = {"cbv", "volunteer", "vtr"}


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def clean_name(raw: str) -> str:
    """Light cleanup used for display grouping: case, accents, punctuation."""
    s = strip_accents(str(raw or "")).lower()
    s = re.sub(r"[^\w\s'-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def name_key(raw: str) -> str:
    """Strong canonical key: drops titles, middle names and initials.

    'Mary Jane A. Okafor' / 'mrs mary j okafor' / 'Mary Okafor' -> 'mary okafor'
    """
    s = clean_name(raw)
    tokens = [t for t in s.split() if t and t not in TITLES and t not in STOPWORDS]
    tokens = [t for t in tokens if len(t) > 1]  # drop initials like 'a.'
    if not tokens:
        return s
    if len(tokens) == 1:
        return tokens[0]
    return f"{tokens[0]} {tokens[-1]}"


def resolve_merge(raw: str, merges: dict[str, str]) -> str:
    """Resolve a name through confirmed merges, including previously merged targets."""
    name = raw
    visited: set[str] = set()
    while True:
        key = name_key(name)
        if not key or key not in merges:
            return name
        if key in visited:
            raise ValueError(f"Name merge cycle detected for {raw!r}")
        visited.add(key)
        target = merges[key]
        if name_key(target) == key:
            return target
        name = target


def display_name(raw: str) -> str:
    return " ".join(w.capitalize() for w in str(raw or "").strip().split())


def tokens(raw: str) -> list[str]:
    return [t for t in clean_name(raw).split() if len(t) > 1]


def key_matches(cbv_key: str, keys) -> bool:
    """Match a canonical key against a set/list of keys.

    Handles inverted names ('alson feston' vs 'feston alson') and
    shortened/lengthened names ('abraham phiri' vs 'abraham samuel phiri').
    """
    if not cbv_key:
        return False
    k_set = frozenset(cbv_key.split())
    for k in keys:
        if k == cbv_key:
            return True
        ks = k.split()
        if frozenset(ks) == k_set:
            return True
        if len(ks) >= 2 and len(k_set) >= 2:
            k2 = frozenset(ks)
            if k2 <= k_set or k_set <= k2:
                return True
    return False


def similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    ka, kb = name_key(a), name_key(b)
    if ka and ka == kb:
        return 100.0
    return max(
        fuzz.WRatio(ka, kb),
        fuzz.token_sort_ratio(ka, kb),
        fuzz.ratio(clean_name(a), clean_name(b)),
    )


def _same_surname_initial(a: str, b: str) -> bool:
    ta, tb = tokens(a), tokens(b)
    if len(ta) < 2 or len(tb) < 2:
        return False
    return ta[-1] == tb[-1] and ta[0][0] == tb[0][0] and ta[0] != tb[0]


def _is_name_subset(a: str, b: str) -> bool:
    """'thomas yosofat' vs 'thomas yosofat chilomba' -> same person (one name shortened)."""
    ta, tb = set(tokens(a)), set(tokens(b))
    if not ta or not tb or ta == tb:
        return False
    if not (ta & tb):
        return False
    return ta <= tb or tb <= ta


def find_duplicates(names: list[str], threshold: float = 88.0) -> list[dict]:
    """Return suspected duplicate groups among unique raw names."""
    uniq = sorted({n.strip() for n in names if n and str(n).strip()})
    parent: dict[str, str] = {n: n for n in uniq}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: str, y: str) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[ry] = rx

    for a, b in combinations(uniq, 2):
        sim = similarity(a, b)
        strong_key = name_key(a) == name_key(b) and bool(name_key(a))
        hit = strong_key or sim >= threshold or _same_surname_initial(a, b) or _is_name_subset(a, b)
        if hit:
            union(a, b)

    groups: dict[str, list[str]] = {}
    for n in uniq:
        groups.setdefault(find(n), []).append(n)

    out = []
    for members in groups.values():
        if len(members) > 1:
            members = sorted(members, key=lambda m: (-len(tokens(m)), m))
            scores = [
                {"a": a, "b": b, "score": round(similarity(a, b), 1)}
                for a, b in combinations(members, 2)
            ]
            out.append({"canonical": members[0], "members": members, "pairs": scores})
    out.sort(key=lambda g: -len(g["members"]))
    return out
