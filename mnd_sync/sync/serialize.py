"""Mapping results as JSON (for the SQLite cache) and a content hash for change detection."""

from __future__ import annotations

import hashlib
import json

from .mapper import MappingResult
from .report import _sum_numeric


def _resolution(r) -> dict:
    return {
        "ingredient": r.source.display or r.query,
        "query": r.query,
        "amount": r.amount,
        "matched": r.matched,
        "mnd_food_id": r.mnd_food_id,
        "mnd_description": r.mnd_description,
        "confidence": r.confidence.value if r.confidence else None,
        "score": r.score,
        "via_alias": r.via_alias,
        "custom_food": r.custom_food,
        "skipped": r.skipped,
        "amount_issue": r.amount_issue,
        "grams": r.grams,
        "calories": r.calories,
        "note": r.note,
        "needs_review": r.needs_review,
    }


def result_to_json(result: MappingResult) -> str:
    recipe = result.recipe
    servings = recipe.servings if recipe else None
    total = _sum_numeric(r.calories for r in result.considered if r.matched)
    per = None
    try:
        if total is not None and servings and float(servings) > 0:
            per = total / float(servings)
    except ValueError:
        pass
    return json.dumps({
        "name": recipe.name if recipe else "",
        "servings": servings,
        "total_kcal": total,
        "kcal_per_serving": per,
        "existing_food_id": result.existing_food_id,
        "resolutions": [_resolution(r) for r in result.resolutions],
    })


def result_from_json(text: str) -> dict:
    return json.loads(text)


def content_hash(raw: dict) -> str:
    return hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()
