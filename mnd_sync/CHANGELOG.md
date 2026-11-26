# Changelog

## 0.2.5

- Log: at start, whether the Supervisor token and time zone reached the add-on; one line per notification sent (no message text), and a warning when one can't be sent.

## 0.2.4

- Meal plan: a meal you deleted in MyNetDiary is logged again if you move it to another day or meal in Mealie (a move is a fresh choice). Left unmoved, it stays deleted.

## 0.2.3

- Fix: a recipe's page gave "Internal Server Error" (MyNetDiary sends grams and calories as text).

## 0.2.2

- Recipes: the last *Refresh from Mealie* result (new, linked, changed…) stays on the page, so it isn't lost when a long refresh finishes after you have moved on. While a refresh runs, the page says so.

## 0.2.1

- Settings: *Test Mealie* and *Test MyNetDiary* now say when nothing is saved yet (press Save first), and the reason for a failed test is written to the add-on log.

## 0.2.0

- Meal plan: log the next 14 days of your Mealie meal plan to the MyNetDiary diary (off by default), with a Meal plan page, a *Log meal plan now* button and notifications.
- Review lists planned meals still waiting for their recipe.
- Recipe page: *Update in MyNetDiary* for changed recipes, *Delete in MyNetDiary too* for recipes deleted in Mealie.

## 0.1.0

First release.

- Mealie recipes to MyNetDiary recipes, with ingredient matching and nutrition checks.
- Review queue for doubtful matches; choices saved as aliases (import `pantry.json`).
- Recipes, Review, Aliases and Settings pages in the Home Assistant sidebar.
- Optional auto-sync (off until you turn it on) for ready, unchanged recipes, with phone notifications.
- Connection tests for Mealie and MyNetDiary; Mealie token created from a login.
