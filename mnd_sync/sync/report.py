"""Render a human-readable review of a Mealie->MND mapping (Markdown).

The report is what you read before committing a sync: what recipe will be
created, whether it duplicates an existing one, and how each ingredient
resolved (alias / confidence / miss).
"""

from __future__ import annotations

import re

from .mapper import MappingResult

_MARK = {True: "OK", False: "MISS"}


_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _sum_numeric(values):
    """Sum the leading number of each value ("210", "210cals", "1,050")."""
    total, seen = 0.0, False
    for v in values:
        m = _NUM_RE.search(str(v or "").replace(",", ""))
        if m:
            total += float(m.group(0))
            seen = True
    return total if seen else None


def render_markdown(result: MappingResult) -> str:
    recipe = result.recipe
    name = recipe.name if recipe else "(none)"
    lines: list[str] = [f"# Sync review: {name}", ""]

    if result.is_duplicate:
        if result.existing_food_id == "planned":
            lines.append(f"> **Duplicate:** another recipe named {name!r} appears "
                         f"earlier in this sync run, so this one is skipped.")
        else:
            lines.append(f"> **Duplicate:** a recipe named {name!r} already exists in "
                         f"MyNetDiary (foodId {result.existing_food_id}). Committing "
                         f"will create a second copy unless you update instead.")
        lines.append("")

    kind = "meal" if (recipe and recipe.meal_not_recipe) else "recipe"
    servings = recipe.servings if recipe else None
    considered = result.considered
    matched = sum(1 for r in considered if r.matched)
    skipped = result.skipped
    lines.append(f"- Type: **{kind}**" + (f" · servings: {servings}" if servings else ""))
    lines.append(f"- Ingredients matched: **{matched}/{len(considered)}**"
                 + (f" · left out: {len(skipped)} (no quantity / to serve / optional)"
                    if skipped else ""))
    total_kcal = _sum_numeric(r.calories for r in considered if r.matched)
    if total_kcal is not None:
        per = ""
        try:
            if servings and float(servings) > 0:
                per = f" · ≈ {total_kcal / float(servings):.0f} kcal per serving"
        except ValueError:
            pass
        lines.append(f"- Total ≈ {total_kcal:.0f} kcal{per}")
    review = result.needs_review
    if review:
        lines.append(f"- **Needs review: {len(review)}** (low confidence or unmatched)")
    lines.append("")

    lines.append("| # | Ingredient | Amount | ≈ g / kcal | → MyNetDiary | Confidence | Note |")
    lines.append("|---|------------|--------|------------|--------------|------------|------|")
    for i, r in enumerate(result.resolutions, 1):
        conf = ""
        if r.via_alias:
            conf = "alias"
        elif r.confidence is not None:
            conf = r.confidence.value
            if r.score is not None:
                conf += f" ({r.score})"
        target = r.mnd_description or "—"
        flag = " ⚠️" if r.needs_review else ""
        mark = "SKIP" if r.skipped else _MARK[r.matched]
        weight = ""
        if r.grams or r.calories:
            weight = f"{r.grams or '?'} g / {r.calories or '?'}"
            g, k = _sum_numeric([r.grams]), _sum_numeric([r.calories])
            if g and k is not None and g >= 5:
                weight += f" ({k / g * 100:.0f}/100g)"
        lines.append(f"| {i} | {r.query or '(empty)'} | {r.amount or '—'} | {weight} | "
                     f"{mark}: {target} | {conf} | {r.note}{flag} |")
    lines.append("")

    if review:
        lines.append("## To review before committing")
        for r in review:
            why = r.note or (f"low confidence ({r.confidence.value})"
                             if r.confidence else "unmatched")
            q = r.query.replace('"', "'")
            if r.matched and r.mnd_food_id and not r.amount_issue:
                fix = (f"If **{r.mnd_description}** is right, accept it: "
                       f"`cli.py alias set \"{q}\" --food-id \"{r.mnd_food_id}\"`. "
                       f"Otherwise find another with `cli.py search \"{q}\" --per-100g`.")
            else:
                fix = (f"Fix the amount/unit in Mealie, or find a food with "
                       f"`cli.py search \"{q}\" --per-100g` and pin it with "
                       f"`cli.py alias set \"{q}\" --food-id <id> [--amount \"...\"]`.")
            lines.append(f"- **{r.query or '(empty)'}** — {why}. {fix}")
        lines.append("")

    if not considered:
        status = "nothing to sync (no usable ingredients in Mealie)"
    elif result.all_matched and not review and not result.is_duplicate:
        status = "READY to commit"
    else:
        status = "review needed"
    lines.append(f"**Status:** {status}.")
    return "\n".join(lines)


_STATUS_LABEL = {
    "ready": "ready",
    "saved": "saved",
    "review": "needs review",
    "duplicate": "duplicate (skipped)",
    "empty": "nothing to sync (skipped)",
    "error": "error",
}


def render_batch_markdown(batch) -> str:
    """Combined review report for a sync-all run (a BatchResult)."""
    mode = "COMMIT" if batch.committed else "dry run"
    out = [f"# Mealie → MyNetDiary sync-all ({mode})", ""]
    n = len(batch.outcomes)
    counts = ", ".join(
        f"{batch.count(s)} {label}" for s, label in _STATUS_LABEL.items()
        if batch.count(s))
    out.append(f"**{n} recipe(s):** {counts or 'none'}")
    if not batch.committed and batch.count("ready"):
        out.append("")
        out.append(f"{batch.count('ready')} recipe(s) would be saved with `--commit`.")
    out.append("")
    out.append("| Recipe | Matched | Left out | Status | Detail |")
    out.append("|--------|---------|----------|--------|--------|")
    for o in batch.outcomes:
        detail = o.error or (f"foodId {o.saved_food_id}" if o.saved_food_id else "")
        if not detail and o.result is not None and o.result.needs_review:
            detail = f"{len(o.result.needs_review)} to review"
        matched = f"{o.matched}/{o.total}" if o.result else "—"
        left = str(o.skipped) if o.result and o.skipped else ""
        out.append(f"| {o.name} | {matched} | {left} | "
                   f"{_STATUS_LABEL.get(o.status, o.status)} | {detail} |")
    out.append("")

    # Every recipe gets a breakdown, ready ones included: a confident match can
    # still be the wrong food, so they deserve a look before --commit.
    detail_outcomes = [o for o in batch.outcomes if o.result is not None]
    if detail_outcomes:
        out.append("## Details")
        out.append("")
        for o in detail_outcomes:
            section = render_markdown(o.result)
            # demote headings two levels so each recipe nests under "## Details"
            section = "\n".join(("##" + line) if line.startswith("#") else line
                                for line in section.splitlines())
            out.append(section)
            out.append("")
    return "\n".join(out).rstrip() + "\n"
