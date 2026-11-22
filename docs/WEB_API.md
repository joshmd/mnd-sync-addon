# MyNetDiary Web API (mui*.do) — V1 reference

Reconstructed from two Firefox HAR captures (food-log + recipe-save).
Auth: JSESSIONID session cookie (web login). Base: https://www.mynetdiary.com
All bodies JSON. KEEP LOCAL — captures contain live session cookies + the
user's own email/health data. NEVER write MND/health data to the Obsidian vault.

## Endpoints inventoried
- POST /muiFoodSearch.do            — search foods; returns opaque versioned foodId tokens
- POST /muiSaveFoodEntry.do         — log a food to a day+meal
- POST /muiCalculateFoodEntryUpdate.do — recompute nutrition when amount changes
- POST /muiDeleteFoodEntry.do       — delete a diary entry
- POST /muiSaveCustomFood.do        — create/update a custom food
- POST /muiSaveRecipe.do            — create/update a recipe OR a meal  ← was the gap, now captured
- GET  /muiLoadRecipe.do            — load recipe editor scaffold
- GET  /muiGetRecipes.do            — list user's recipes
- GET  /muiGetFoodEntry.do, /muiGetMeals.do, /muiGetRecentAndPopularFoods.do, /muiGetPlan.do, /muiGetDashboard.do, /muiLoadCustomFood.do, /muiValidateCustomFoodServingSize.do

## Search — POST /muiFoodSearch.do   [VERIFIED]
Request:  {"searchToken":"ban","skipLabel":true}
Response: {"foundFoods":[ {foodId, foodName, foodDesc, calories, carbs, amount, imageSrc, ...}, ...], ...}
- skipLabel:true => NO foodLabel/nutrition in results (lightweight list).

## Resolve a food (get foodLabel) — GET /muiGetFoodEntry.do   [VERIFIED]
  ?mealTypeId=1&foodId=<token-or-numeric>&amountInputString=2+medium
Response: {"foodEntry":{ foodId, foodDesc, foodLabel{...full nutrition...}, calories, carbs, gmWgt, ... }}
- This is how a search token becomes a labelled ingredient for a recipe.

## Log a food — POST /muiSaveFoodEntry.do   [VERIFIED]
Two forms:
  inline (quick add, current day):
    {"inlineEntry":true,"mealTypeId":1,"foodId":"v1...=","amountInputString":"medium","searchToken":"bana"}
  full (arbitrary day / edit):
    {"foodId":"v1...=","day":"2026-11-21","timeMinutes":1,"mealTypeId":1,"timeString":"0:01",
     "amountInputString":"2 medium","entryNo":101,"initialDay":"2026-11-21","inputString":"Banana"}
- day: YYYY-MM-DD => arbitrary-day logging CONFIRMED.
- entryNo: diary row id; omit to create, pass to edit. Also used by delete.
- mealTypeId (confirmed from muiGetMeals.do): 1=Breakfast, 2=Lunch, 3=Dinner, 4=Snacks.

## Delete — POST /muiDeleteFoodEntry.do   [VERIFIED]
  {"day":"2026-11-21","entryNo":101}

## Recompute amount — POST /muiCalculateFoodEntryUpdate.do   [VERIFIED]
  {"foodId":"80214011","amountInputString":"1 serving"}  ->  {"foodEntryUpdate":{...foodLabel...}}

## Custom food — POST /muiSaveCustomFood.do   [VERIFIED]
  {"customFood":{"CALORIES":{"nutrDesc":"Calories","required":true,"displayUnits":"cals","nutrValue":"100","error":null},
                 "TOTAL_FAT":{...}, "SAT_FAT":{...}, ...}}

## Save a recipe or meal — POST /muiSaveRecipe.do   [SOLVED]
Request body: { "recipe": { ...recipe object... } }

Top-level recipe fields:
- mealNotRecipe : false = RECIPE (reusable, has servings), true = MEAL (one-off bundle)
- name          : recipe/meal name (string)              [required]
- serving       : number of servings as string, e.g. "2" (recipe only)
- imageId       : icon id (14 default); full list in recipe.images[]
- foods[]       : ingredient array (see below)           [required]
- calories      : total kcal as string
- weight        : total grams (number)
- ingredientsWeight : grams of ingredients (number)
- prepTime, cookingTime, cookingInstructions : optional strings
- *Error fields (nameError, servingError, ...) : null on valid input; server fills on error

Each foods[] element = a muiFoodSearch.do result object plus amount/ordinal:
  { "foodId":"v1....=",            // same opaque search token used for logging
    "foodDesc":"Banana",
    "foodLabel":{...full nutrition block from search...},
    "calories":"210","rawCalories":210,
    "amount":"2 medium",           // serving string chosen for this ingredient
    "rawGmWgt":236,
    "carbs":{"TotalCarbs":"54","NetCarbs":"48","DiabetesCarbs":"51"},
    "imageSrc":"https://assets.mynetdiary.com/bean/i611.png",
    "ordinal":1 }

Response (on commit):
  { "foodId":"80214011",           // NEW permanent numeric id of saved recipe/meal
    "recipeMyFood":{ foodDesc, calories, carbs, foods[], ... },
    "securityToken":"...", "userType":"Free", ... }
- The returned numeric foodId is then loggable like any food via muiSaveFoodEntry.do.
- Recipe commit → foodId 80214011 ("test recipe"); Meal commit → 80214032 ("test meal").

Observed sequence when saving in the UI: an intermediate /muiSaveRecipe.do that
echoes the full recipe (validate/recalc, no top-level foodId), then a final one
that returns the numeric foodId. For programmatic use, send the complete recipe
object once; read foodId from the response.

## Mealie -> MND sync (Route 2, robust)
For each Mealie ingredient: muiFoodSearch.do -> take best result object ->
set amount -> push into foods[] (ordinal 1..n) -> POST muiSaveRecipe.do with
mealNotRecipe:false, name, serving. No web-scrape/URL-import needed.
("Import Web Recipe" UI exists but user confirmed it's hard to find / fragile;
 they confirmed Save works.)

## V1 status: ALL FOUR CAPABILITIES CAPTURED
  log food (muiSaveFoodEntry) · delete (muiDeleteFoodEntry) ·
  custom food (muiSaveCustomFood) · recipe/meal (muiSaveRecipe)  ← complete

## Login (verified live 2026-11-22)

Unattended login works through the **mobile app endpoint**, and the resulting
session is accepted by the web `mui*.do` endpoints.

- `POST https://www.mynetdiary.com/ipActivation.do` (form-urlencoded):
  `logonName` (account email), `password`, `rememberMe=true`, plus the Android
  device fields (`appVersion 9.9.2`, device ids, OS info); headers
  `User-Agent: okhttp/4.12.0`, `Android-Version-Code: 179220`.
- Response: MyNetDiary's `OK … \`+\` …` text format. Fields seen: `_status`,
  `mobileAttrs`, `needsDiabetesInfo`, `setupInfo`, `status`, `userPlan`,
  `userSettings`. `ERROR …` on bad credentials.
- Cookies set: `JSESSIONID`, `rememberMe`, `AWSALB`, `AWSALBCORS`.
- Using the same cookie jar, `muiGetRecipes.do` returns account data
  (`userType`/`securityToken` present) — the session is web-valid.
- Reference implementation: `scripts/login_spike.py` (`mobile_login`).
- Device ids should be persisted per installation so repeated logins register
  one device.

**Decision:** the add-on uses `password` mode (mobile login). The pasted-cookie
mode remains as a fallback.

## Part 2 endpoints (captured 2026-11-22)

- `GET muiGetMeals.do?date=YYYY-MM-DD` — switches the session's diary day.
  Response `meals[]` (`mealTypeId`, `foodEntries[]` with `entryNo`, `foodName`/`foodDesc`).
- `POST muiSaveFoodEntry.do` (full form, other day) — body `foodId`, `day`,
  `timeMinutes`, `mealTypeId`, `timeString`, `amountInputString`, `searchToken`.
  Response: `entryNo` plus the meals payload.
- `GET muiLoadRecipe.do?recipeId=<foodId>` — `{"recipe": {...}}` (`foodId`, `name`,
  `foods`, `serving`, `entriesNum`, ...).
- `POST muiSaveRecipe.do` `{"recipe": {..., "foodId": <int>}, "sortOrder": "a"}` —
  updates in place. Response `foodId` (string) and `recipeMyFood`.
- `GET muiRetireMyFood.do?foodId=<id>` — response `{"success": true}`.
- `POST muiDeleteFoodEntry.do` `{day, entryNo}` — deletes a diary entry.
