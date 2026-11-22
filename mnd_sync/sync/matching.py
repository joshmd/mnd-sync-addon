"""Fuzzy matching of a Mealie ingredient name to MyNetDiary search results.

MyNetDiary search returns many candidates; naive "first hit" is unreliable
(a search for "banana" can surface "banana bread" first). We score each
candidate against the query by token overlap + sequence similarity, pick the
best, and attach a confidence band so low-quality matches can be held back for
review instead of silently committed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import Enum
from typing import Optional, Sequence

# Words that add no matching signal in food names.
_STOPWORDS = {
    "fresh", "raw", "organic", "whole", "of", "the", "a", "an", "and", "or",
    "with", "without", "large", "medium", "small", "chopped", "sliced",
    "diced", "minced", "ground", "to", "taste", "crushed", "grated", "dry",
    "dried", "style",
}
# Preparation words dropped from the *search* query when a first search is
# weak ("frozen chopped chilli" -> "chilli").
_PREP_WORDS = _STOPWORDS | {
    "frozen", "tinned", "canned", "cooked", "boneless", "skinless", "peeled",
    "finely", "roughly", "thinly", "cubed", "halved", "quartered", "softened",
    "melted", "beaten", "shredded", "cold", "hot", "warm", "lean", "extra",
}
# British/US spelling variants mapped to one form before comparing.
_SPELLING = {
    "chilli": "chili", "chillies": "chili", "chilies": "chili",
    "yoghurt": "yogurt", "yoghurts": "yogurt", "aubergine": "eggplant",
    "aubergines": "eggplant", "coriander": "coriander", "mince": "minced",
}
# Words naming a prepared dish or product. A candidate containing one that the
# ingredient doesn't mention is usually a different food ("mint" -> "Mint tea",
# "red lentil" -> "Red lentil soup", "cheese" -> "Mac & cheese").
_DISH_WORDS = {
    "soup", "dal", "dhal", "quiche", "snack", "tea", "chocolate", "cake",
    "cookie", "biscuit", "pie", "pizza", "sandwich", "bar", "salad", "curry",
    "mac", "muffin", "crisp", "chip", "juice", "smoothie", "cereal", "drink",
    "latte", "wrap", "burger", "nugget", "lasagne", "stew", "casserole",
    "risotto", "dessert", "pudding", "tart", "pasty", "flavour", "flavoured",
    "flavored", "spread", "jam", "candy", "gum", "shake", "milkshake",
    # soft drinks ("pepper" -> "Dr. Pepper")
    "dr", "cola", "lemonade", "pop", "fizzy",
}
DISH_PENALTY = 0.30
_TOKEN_RE = re.compile(r"[a-z0-9]+")
# A trailing brand, as MyNetDiary appends it: "Fusilli by asda".
_BRAND_RE = re.compile(r"\s+by\s+[^,()]+$", re.IGNORECASE)


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


# Score thresholds for the confidence bands. A one-word ingredient against a
# two-word name ("pepper" vs "Dr. Pepper", "onion" vs "Red onion") scores
# ~0.78-0.81, so HIGH starts above that: such matches get reviewed.
HIGH_THRESHOLD = 0.85
MEDIUM_THRESHOLD = 0.55


def _stem(token: str) -> str:
    """Crude singular form, so "peas"/"pea" and "tomatoes"/"tomato" compare equal."""
    t = _SPELLING.get(token, token)
    if len(t) > 4 and t.endswith("ies"):
        t = t[:-3] + "y"
    elif len(t) > 4 and t.endswith(("oes", "ches", "shes")):
        t = t[:-2]
    elif len(t) > 3 and t.endswith("s") and not t.endswith(("ss", "us", "is")):
        t = t[:-1]
    return _SPELLING.get(t, t)


def _tokens(text: str) -> list[str]:
    text = _BRAND_RE.sub("", (text or "").lower())
    out = []
    for raw in _TOKEN_RE.findall(text):
        if raw in _STOPWORDS:
            continue
        t = _stem(raw)
        if t not in _STOPWORDS:
            out.append(t)
    return out


_DISH_STEMS = {_stem(w) for w in _DISH_WORDS}


def simplify_query(query: str) -> str:
    """Drop preparation words and normalise spelling for a fallback search."""
    words = [w for w in _TOKEN_RE.findall((query or "").lower()) if w not in _PREP_WORDS]
    return " ".join(_SPELLING.get(w, w) for w in words)


def score(query: str, candidate: str) -> float:
    """Similarity in [0, 1] between an ingredient name and a food name.

    Identical word sets (after stopwords, plurals and brand removal) score 1.
    Otherwise it blends how much of the ingredient the candidate covers, how
    much of the candidate is the ingredient (penalising "Spinach & tomato
    quiche" for "tomato"), and character similarity, minus a penalty for each
    prepared-dish word the ingredient doesn't mention.
    """
    q_tokens, c_tokens = _tokens(query), _tokens(candidate)
    if not q_tokens or not c_tokens:
        return 0.0
    qs, cs = set(q_tokens), set(c_tokens)
    if qs == cs:
        return 1.0
    inter = qs & cs
    if not inter:
        return 0.0
    coverage = len(inter) / len(qs)    # how much of the ingredient is found
    precision = len(inter) / len(cs)   # how much of the candidate is the ingredient
    seq = SequenceMatcher(None, " ".join(q_tokens), " ".join(c_tokens)).ratio()
    s = 0.5 * coverage + 0.3 * precision + 0.2 * seq
    s -= DISH_PENALTY * len((cs - qs) & _DISH_STEMS)
    return max(0.0, min(1.0, s))


def band(s: float) -> Confidence:
    if s >= HIGH_THRESHOLD:
        return Confidence.HIGH
    if s >= MEDIUM_THRESHOLD:
        return Confidence.MEDIUM
    return Confidence.LOW


# A database food within this much of the best custom food's score wins.
SYSTEM_PREFERENCE_MARGIN = 0.15


def _is_system(cand) -> bool:
    return bool(getattr(cand, "is_system", False))


@dataclass
class Match:
    candidate: object  # a FoodSearchResult (duck-typed: .food_id, .description)
    score: float
    confidence: Confidence

    @property
    def is_system(self) -> bool:
        return _is_system(self.candidate)


def best_match(query: str, candidates: Sequence, *,
               prefer_system: bool = True) -> Optional[Match]:
    """Return the best candidate as a Match, or None if empty.

    ``candidates`` are objects with ``.description`` (and ``.food_id``,
    optionally ``.is_system``). Ties keep search order.

    With ``prefer_system`` (default), if the top scorer is a user custom food
    but a MyNetDiary database food scores at least MEDIUM and within
    SYSTEM_PREFERENCE_MARGIN of it, the database food is chosen instead.
    Custom foods often lack per-100g data and reject generic amounts such as
    "1 medium" or "150 g", so database foods are the safer default.
    """
    scored = [(score(query, getattr(c, "description", "") or ""), c)
              for c in candidates]
    if not scored:
        return None
    best_s, best_c = max(scored, key=lambda t: t[0])  # first max wins ties
    if prefer_system and not _is_system(best_c):
        system = [(s, c) for s, c in scored if _is_system(c)]
        if system:
            sys_s, sys_c = max(system, key=lambda t: t[0])
            if sys_s >= MEDIUM_THRESHOLD and sys_s >= best_s - SYSTEM_PREFERENCE_MARGIN:
                best_s, best_c = sys_s, sys_c
    return Match(candidate=best_c, score=best_s, confidence=band(best_s))


def ranked_matches(query: str, candidates: Sequence, *,
                   prefer_system: bool = True) -> list[Match]:
    """All candidates as Matches, best first (``best_match`` decides the top,
    including the database-food preference; the rest follow by score)."""
    top = best_match(query, candidates, prefer_system=prefer_system)
    if top is None:
        return []
    rest = [(score(query, getattr(c, "description", "") or ""), i, c)
            for i, c in enumerate(candidates) if c is not top.candidate]
    rest.sort(key=lambda t: (-t[0], t[1]))
    return [top] + [Match(candidate=c, score=s, confidence=band(s)) for s, _, c in rest]
