# MyNetDiary Sync

Copies your Mealie recipes into MyNetDiary as recipes, so you can log them there.
It matches each ingredient to a MyNetDiary food, lets you review doubtful
matches, remembers your choices (aliases), and can sync automatically.

## Install

1. In Home Assistant: Settings, Add-ons, Add-on store, the three-dot menu,
   Repositories. Add `https://github.com/joshmd/mnd-sync-addon`.
2. Install "MyNetDiary Sync", press Start, then open it from the sidebar
   (MyNetDiary).

## First-time setup (Settings page)

- **Mealie URL**: the default `http://a0d7b954-mealie:9000` is for the Mealie
  add-on; no port needs opening.
- **Mealie login**: enter your username and password once to create an API
  token (the password is not stored), or paste an API token instead.
- **MyNetDiary**: email and password. They are stored in the add-on's private
  storage so it can log in by itself.
- Press **Test Mealie** and **Test MyNetDiary**.
- On the Recipes page press *Refresh from Mealie*. Recipes you already have in
  MyNetDiary (same name) show as *saved*. The message says how many were
  linked; if it says "0 linked" but you do have recipes in MyNetDiary, check
  the account before going on, or they would be saved a second time.
- **Phone notification service** (optional): the part after `notify.`, for
  example `mobile_app_my_phone`.

## Using it

- **Recipes**: *Refresh from Mealie* loads the list; the *Show* links filter it
  by status. *Sync all ready* saves every ready recipe. Or tick recipes, then
  *Check selected* (dry run, saves nothing), *Sync selected*, or *Force sync
  selected* (saves recipes that still need review, leaving out unmatched
  ingredients; asks you to confirm). A recipe's own page has *Sync this recipe*.
- **Review**: for each ingredient that needs a decision, accept the suggested
  food, choose another food (shown with kcal per 100 g), or leave it out.
  Choose "All recipes" to remember it for every recipe, or "This recipe only".
- **Aliases**: edit or delete saved choices. You can import an existing
  `pantry.json` by pasting its contents.

## Auto-sync

Auto-sync is **off** until you tick it on the Settings page. Turn it on after
the first Refresh shows your existing recipes as saved.

When on, every 30 minutes the add-on refreshes from Mealie and saves only
recipes that are ready and have not changed for 60 minutes. Everything else
waits for your review. You get one notification per change. Change the
interval (*Check Mealie every ... minutes*) and the quiet period (*Save a
recipe once it has not changed for ... minutes*) on the Settings page; both
are whole minutes, 1 or more. *Run sync now* runs the same check immediately.
Problems are written to the add-on log (Settings, Add-ons, MyNetDiary Sync, Log).

## Meal plan

With *Log meal plan to diary* ticked (Settings, **off** by default), the add-on
adds the next 14 days of your Mealie meal plan to the MyNetDiary diary, 1
serving each. Mealie's meal types map to diary meals: breakfast to Breakfast,
lunch to Lunch, dinner, side and brunch to Dinner, snack, dessert and drink to Snacks. Plan entries that
are only a note are not logged. A planned recipe that has not synced yet waits
(see the Meal plan page and Review) and is added once it syncs.

Try it first: leave the setting off and press *Log meal plan now* on the Meal
plan page for a real week, then check the diary. Only then turn it on.

- An entry you delete in MyNetDiary is not added again, unless you then move that meal to another day or meal in Mealie: a move logs it on the new day.
- If you move or remove a plan entry in Mealie, the diary follows.
- Past days are never touched.

On a recipe's page, *Update in MyNetDiary* (for a recipe that changed in
Mealie) updates the recipe in place or, if it is already in your diary, saves
a new copy and retires the old one (past days keep the old version; planned
meals from today on move to the new one). *Delete in MyNetDiary too* removes a recipe you
deleted in Mealie.

## Privacy

Credentials stay in the add-on's private storage. Home Assistant backups
include it, so turn on backup encryption. The web page only answers Home
Assistant's ingress (the sidebar panel); direct requests from other add-ons get "Forbidden".

## Limits

- This uses MyNetDiary's unofficial web API and may break if MyNetDiary changes it.
- The MyNetDiary phone app may need a sync or a search before new recipes show up.
