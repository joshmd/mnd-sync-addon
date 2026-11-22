# MyNetDiary Sync (Home Assistant add-on)

A Home Assistant add-on repository. The add-on copies Mealie recipes into
MyNetDiary as recipes, with a review queue for doubtful ingredient matches.

## Install

In Home Assistant: Settings, Add-ons, Add-on store, the three-dot menu,
Repositories, add `https://github.com/joshmd/mnd-sync-addon`, then install
"MyNetDiary Sync". Setup and usage: [`mnd_sync/DOCS.md`](mnd_sync/DOCS.md).

## Command-line tool

`mnd_sync/cli.py` also works on its own, without Home Assistant (see
"Usage" below). Writes are dry-run unless `--commit`.

> **Privacy:** never commit HAR captures, session cookies or logged-food data.

## Layout
Home Assistant add-on repository: `repository.yaml` at the root, the add-on in
`mnd_sync/` (`config.yaml`, `Dockerfile`, `app/` = FastAPI ingress UI). Run the
CLI and tests from `mnd_sync/`. Paths below are inside `mnd_sync/`.
```
app/             # add-on web UI (FastAPI, served via HA ingress on :8099)
mynetdiary/      # the MND web API client
  client.py      #   MndClient: auth, search, log, delete, recipe, custom food
  models.py      #   FoodSearchResult, FoodEntry, Ingredient, Recipe, SavedFood
  errors.py
mealie/          # minimal Mealie REST client + recipe model
sync/            # Mealie -> MND mapper (Route 2: build from structured ingredients)
  mapper.py      #   Mealie recipe -> MND recipe, with alias/match/confidence
  matching.py    #   fuzzy ingredient matcher + confidence bands
  units.py       #   Mealie quantity+unit -> MND amount string
  aliases.py     #   persistent ingredient override map (pantry.json)
  report.py      #   Markdown review report for a dry-run sync
cli.py           # command-line interface; writes default to --dry-run
tests/           # offline unit tests (models, mapper, sync logic; no network)
```

## How the Mealie sync resolves ingredients (Route 2)
Per ingredient, in order:
1. **Alias map** — a recorded override (`pantry.json`) wins outright, so a
   once-corrected match (`maida` -> white flour) is deterministic thereafter.
2. **Search** — fetch several MND candidates for the ingredient name.
3. **Match** — fuzzy-score candidates (token coverage + sequence similarity) and
   take the best, with a confidence band (high / medium / low).
4. **Resolve** — turn the chosen foodId + amount into a labelled food entry.

**Database vs custom foods.** The matcher prefers a MyNetDiary database food
whenever one scores at least *medium* and within 0.15 of the best custom food.
Custom foods often lack per-100g data and reject generic amounts ("1 medium",
"150 g"), so when a custom food is still the best match it is flagged for
review. To accept it deliberately, pin it with an alias (aliases are trusted).

The sync is **read-only until you commit**. It renders a review report (see
sample) and, on `--commit`, refuses to save if any ingredient needs review or a
recipe of that name already exists — unless you pass `--force`.

## Auth
The CLI needs a MyNetDiary session. Copy the `JSESSIONID` cookie from a
logged-in browser session (the add-on logs in by itself):
```
export MND_JSESSIONID=<cookie value>
```

## Usage (writes are dry-run unless --commit)
```
cd mnd_sync
pip install -r requirements.txt
python cli.py search "banana"
python cli.py log --food-id "v1...=" --meal 1 --amount "2 medium"            # dry-run
python cli.py log --food-id "v1...=" --meal 1 --amount "2 medium" --commit
python cli.py recipe --name "Smoothie" --servings 2 \
    --ingredient "banana=2 medium" --ingredient "milk=1 cup"                 # dry-run
python cli.py delete --day 2026-11-21 --entry-no 101 --commit
# Mealie sync (MEALIE_URL is Mealie's address, e.g. http://<host>:9000)
export MEALIE_URL=http://192.168.1.10:9000  MEALIE_TOKEN=...
python cli.py mealie-check                                                  # test URL + token
python cli.py sync-recipe --slug my-recipe                                   # dry-run + report
python cli.py sync-recipe --slug my-recipe --report review.md                # save the report
python cli.py sync-recipe --slug my-recipe --commit                          # blocks if review needed
python cli.py sync-recipe --slug my-recipe --commit --force                  # override the block
# Sync every Mealie recipe:
python cli.py sync-all --report sync-all.md                                  # dry run + combined report
python cli.py sync-all --only my-recipe --only other-recipe                  # just these slugs
python cli.py sync-all --commit                                              # save only 'ready' recipes
python cli.py sync-all --commit --force                                      # also save 'needs review' ones
# pin a stubborn ingredient so future syncs are deterministic:
python cli.py alias set "spirulina" --food-id 40123456 --amount "1 tsp"
python cli.py alias list
```

## Tests
```
python -m pytest tests/ -q      # or: python tests/test_models.py
```

## Amount verification
MyNetDiary parses amount text loosely and silently misreads some of it
(a live run turned "16" chicken thighs into 16 g, "1.3 kg" into 100 g and
"1200 ml" into 1 g). So each amount's expected weight is worked out from the
food's own measures (`foodWeights`) or an exact mass conversion. If
MyNetDiary's grams differ by more than 10%, the amount is re-sent in grams and
checked again. Counts with no count measure, volumes at an implausible
density, and suspicious can/clove/slice weights are flagged for review. The
report's "≈ g / kcal" column and notes show how each amount was resolved.

Small differences from MyNetDiary rounding (±1 g) are accepted. When the
top match lacks the needed measure (no tsp, no count, no can), up to three
near-equal alternatives are tried. If an amount can still only be estimated
(1 g/ml for spoons and cups, 400 g per can), the estimate is sent and the
line is flagged, unless it is worth 25 kcal or less (most spices), in which
case it is just noted.

**Bad food entries.** A correct name match can still have wrong nutrition
(a live run found a "Parmesan cheese" entry at ~111 kcal/100 g). The report
shows kcal/100 g per line; to replace an entry, compare candidates with
`cli.py search "parmesan cheese" --per-100g` and pin one with
`cli.py alias set "parmesan cheese" --food-id <id>`.

Only **high**-confidence matches (score ≥ 0.85, or aliases) pass unreviewed. Serving
suggestions are left out only when they have no quantity; an ingredient with
a real quantity is included even if Mealie marks it "to serve" or "optional".

## sync-all rules
Each recipe is classified as **ready**, **needs review**, **duplicate**,
**no matches**, or **error**. With `--commit`:
- only **ready** recipes are saved by default;
- `--force` also saves **needs review** recipes (unmatched ingredients are left out);
- **duplicates** (name already in MyNetDiary, or earlier in the same run) are
  skipped unless `--allow-duplicates`;
- recipes with **no matches** are never saved;
- one recipe failing never stops the run (the exit code is 1 if any errored).

`--delay` (default 1s) pauses between saves to be gentle on MyNetDiary.

## Live validation (CLI; run on a machine that can reach mynetdiary.com)
Confirms the reconstructed request shapes work against a real account, in
order of increasing side-effects. Needs a browser session cookie.
```
export MND_JSESSIONID=<JSESSIONID cookie from a logged-in browser>
python scripts/validate_live.py                 # Stage A: READ-ONLY (auth/search/resolve)
python scripts/validate_live.py --write-test    # + log a test food, read back, DELETE it
python scripts/validate_live.py --recipe-test   # + save one test recipe (delete it in the UI)
```
Stage B is fully reversible (it deletes what it logs) and also confirms the
mealTypeId map by reading which meal each entry lands in. Stage C leaves one
recipe named `ZZZ VALIDATION (delete me)` because no recipe-delete endpoint is
captured yet — remove it via Me → Manage My Foods → Recipes.

## Verified endpoint shapes
See [`docs/WEB_API.md`](docs/WEB_API.md). Summary:

| Capability   | Endpoint                      | Key body/params |
|--------------|-------------------------------|-----------------|
| Search       | POST muiFoodSearch.do         | `{searchToken, skipLabel:true}` → `foundFoods[]` |
| Resolve food | GET  muiGetFoodEntry.do       | `?mealTypeId&foodId&amountInputString` → `foodEntry` (has foodLabel) |
| Log food     | POST muiSaveFoodEntry.do      | create: `{inlineEntry:true, mealTypeId, foodId, amountInputString, searchToken}` → `foodEntry.entryNo`; edit: `{foodId, day, entryNo, ...}` |
| Delete       | POST muiDeleteFoodEntry.do    | `{day, entryNo}` |
| Recipe/meal  | POST muiSaveRecipe.do         | `{recipe:{name, serving, mealNotRecipe, foods[]}}` → new numeric `foodId` |
| Custom food  | POST muiSaveCustomFood.do     | `{customFood:{<NUTR>:{nutrValue,...}}}` |

## Live validation status (verified against a real account)
| Path | Status |
|------|--------|
| Auth via JSESSIONID cookie | ✅ verified |
| Search (`muiFoodSearch.do`) | ✅ verified |
| Resolve labelled food (`muiGetFoodEntry.do`) | ✅ verified |
| Log food, inline create, session day | ✅ verified (all 4 meals) |
| mealTypeId map 1/2/3/4 = Breakfast/Lunch/Dinner/Snacks | ✅ verified |
| Delete diary entry (`{day, entryNo}`) | ✅ verified |
| Recipe save (`muiSaveRecipe.do`, our generated body) | ✅ verified |

## Known gaps / TODO
- **Web login** → JSESSIONID not captured. Workaround: browser cookie.
- **Logging to an arbitrary day**: the full body with `day` is rejected for new
  entries (it is the edit form). Inline create logs to the session's current
  day. Backdating needs the date-switch or create-then-edit flow captured.
- **Custom foods** without per-100g data reject generic amounts. Handled by
  preferring database foods and flagging custom picks for review; the accepted
  unit string for custom foods ("'food' units") is still not pinned down.
- **Recipe delete** endpoint not captured (remove test recipes in the UI).
- **Custom food create** body modelled loosely; add a nutrient builder when needed.

