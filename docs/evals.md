# Agent evals

Agent quality is judged by database state, never by wording. Each case in `tests/evals/*.yaml`
seeds a household, sends its turns through `simulate_turn()` (the service the dashboard Playground
uses), and asserts on rows.

The clock is pinned: every case starts on Monday 5 October 2026 at 12:00 in London, and each
further turn is a minute later. "On Wednesday" is therefore always 7 October, whenever the suite
runs. A case can set `now: "2026-10-19 12:00"` to start elsewhere.

## Running

Evals call real models and cost money, so they are skipped unless `RUN_EVALS=1`. They are not
part of CI. They use the same `houseagent_test` database as the other tests.

```sh
RUN_EVALS=1 \
EVAL_API_KEY=... EVAL_MODEL=... \
EVAL_OPENAI_BASE_URL=https://provider.example/v1 \
EVAL_ANTHROPIC_BASE_URL=https://provider.example/anthropic \
uv run pytest tests/evals
```

Every case runs once per endpoint that is configured, so the same suite exercises both adapters.
Results, with token totals, are written to `tests/evals/.results/<provider>.json` (git-ignored).
Add `-k "calendar or reminders"` to run part of the suite.

## Adding a case

```yaml
- name: batch finish with staple
  seed:
    items: [{name: egg, staple: true, stock: {fridge: 6}}, {name: bread, stock: {store: 1}}]
    list: []                      # item names already on the shopping list
  turns: [{from: Ola, text: "we're out of eggs and bread"}]   # from: Ola or Ada; scope: dm (default) or group
  # a turn may also carry photos: [receipt_supermarket.png], files in tests/evals/fixtures/
  expect:
    events: [{item: egg, type: finished}, {item: bread, type: finished}]
    shopping_list_active: [egg]
    reply: ACK
```

| `expect` key | Asserts |
| --- | --- |
| `events` | Exactly these inventory events (undo markers excluded). `type` may be a list of accepted types; `quantity` and `location` are checked when given |
| `shopping_list_active` | Exactly these items are on the list |
| `stock` | Per item: `status`, and `qty` and `location` when given |
| `writes: 0` | No events, no logged actions, and the list is unchanged from the seed |
| `reply` | The last turn's outcome: `ACK`, `NOOP`, or `text` for any other reply |
| `calendar` | Exactly these active events, in any order. Each may give `title_contains`, `participants`, `local_start` ("Wed 7 Oct 11:00"), `location_contains`, `rrule_contains`, `repeats: false`, `exdates` |
| `calendar_cancelled` | How many events are cancelled |
| `reminders_scheduled` | How many reminder rows are scheduled, for events and standalone |
| `reminders` | Exactly these scheduled standalone reminders: `text_contains`, `local_fire`, `target`, `member`, `urgency`, `rrule_contains`, `repeats: false` |
| `events_include` | Photos: an event whose item name contains `item_contains`, of one of the `type`s, exists. A model may call the item "milk" or "semi skimmed milk" |
| `events_all` | Every event has this `type`, `source` or `location` (a value or a list of accepted values) |
| `events_max` | No more events than this, so totals, savings and invented items fail the case |
| `members_added` | Exactly these people beyond Ola and Ada, with their roles |
| `onboarding_done`, `onboarding_step` | Steps that must be done; the step setup is now on (`null` when complete) |
| `staples`, `shops`, `facts`, `facts_mention` | Items flagged as staples; shop names containing each text; a fact by key whose value contains the text; any fact value containing the text |
| `brief`, `quiet` | The brief time (`"07:00"`); quiet hours per adult (`"22:00-06:30"`) |
| `invites_sent` | How many invite messages were queued, each to the person who asked |

Item names are normalised the way the resolver does, so `eggs` and `egg` are the same item.

Calendar cases can seed people and events:

```yaml
- name: skip one week of a recurring activity
  seed:
    members: [{name: Tobi, role: child}]
    events: [{title: Chatterbox, starts_at: "2026-10-06 09:00", rrule: "FREQ=WEEKLY;BYDAY=TU", participants: [Tobi]}]
  turns: [{from: Ada, text: "no Chatterbox tomorrow, it's half term"}]
  expect:
    calendar: [{title_contains: Chatterbox, rrule_contains: FREQ=WEEKLY, exdates: ["2026-10-06"]}]
    reminders_scheduled: 0
```

Seeded events go through the calendar service, so they have their reminders, but leave no undo
record. The spec's example uses the key `events` for calendar events; that key already meant
inventory events here, so calendar expectations are under `calendar`.

## Suite

45 cases: inventory (8), shopping list (5), NOOP (3), undo (4), calendar (7), reminders (4),
photos (5: three receipts, two fridge or freezer photos) and onboarding, family and settings (9).
The spec's target is 40. Release bar: 95% overall and 100% on the NOOP and undo cases, on at
least two providers.

The photo fixtures in `tests/evals/fixtures/` are drawn by `make_fixtures.py` in that folder
(`uv run --with pillow python tests/evals/fixtures/make_fixtures.py`). None is a real photo. A
photo case reads its image through a read-only `MediaStore` over that folder, so the image takes
the same path into the model as a stored Telegram photo.

## Latest results

One run of the whole suite, 6 Oct 2026 (milestone 4), model `deepseek-flash` through both
adapters. Nothing in `app/agent/` changed since the previous run, and no case was added: the
agent does not behave differently on WhatsApp, and the suite talks to it without a channel.

| Adapter | Endpoint | Passed | NOOP | Undo | Input tokens (cached) | Output tokens |
| --- | --- | --- | --- | --- | --- | --- |
| `openai_compat` | `https://api.deepseek.com/` | **42/45** (93.3%) | 3/3 | **3/4** | 413,495 (393,344) | 12,688 |
| `anthropic` | `https://api.deepseek.com/anthropic` | **41/45** (91.1%) | 3/3 | **3/4** | 405,192 (385,280) | 11,885 |

| Category | `openai_compat` | `anthropic` |
| --- | --- | --- |
| inventory | 7/8 | 7/8 |
| shopping list | 5/5 | 5/5 |
| NOOP | 3/3 | 3/3 |
| undo | 3/4 | 3/4 |
| calendar | 7/7 | 7/7 |
| reminders | 4/4 | 4/4 |
| photos | 4/5 | 4/5 |
| onboarding | 9/9 | 8/9 |

The release bar (95% overall and 100% on NOOP and undo, on at least two providers) is **not met
on either endpoint** in this run. The previous run, on the same agent code, scored 44/45 and
41/45 with undo at 4/4 on both. The difference is the model answering differently, which is what
one sample per case cannot average out.

Cost: the account balance, shown to the cent, read $4.85 before the run and $4.84 after it.

Failures, from the assertion messages of this run:

| Case | Endpoint | What the rows showed |
| --- | --- | --- |
| batch finish with staple | both | Bread was on the shopping list as well as eggs. Bread is not a staple: the model added it itself. Seen in every run so far |
| undo | `openai_compat` | After "finished the rice" then "undo", rice was still on the shopping list |
| correction undoes then records what was meant | `anthropic` | After "we're out of milk" then "no wait, I meant we're running low", milk was `low` with quantity 0, not 2: consistent with `low` being logged without the undo first |
| long receipt ignores totals and savings | `openai_compat` | Seven items logged from the receipt, rice as a new item "basmati rice", then "rice" restocked again with source `shopping`: the double recording described under the previous run |
| fridge photo adjusts what is visible and finishes nothing | `anthropic` | The five items were logged twice: first with four of them in `store`, then all five in `fridge`. Stock ended right for the fridge, with stray rows in `store` |
| quiet hours changed later just by talking | `anthropic` | Ada said "don't message me after 8pm" and both adults ended at 20:00 to 07:00. Also failed in the previous run |

The two undo cases were each run once more with tool calls printed, to see whether the undo code
or the model was at fault. Both passed that time: `undo_last` was called and restored the rows
exactly ("undo"), and `undo_last` then `log_inventory` low gave quantity 2 ("correction"). So the
undo machinery does what it should when it is called; in the scored run the model did something
else. Those two re-runs are not part of the score.

## Previous full run (milestone 3)

One run of the whole suite, 6 Oct 2026, model `deepseek-flash` through both adapters, with all
eleven everyday tools on offer (twelve in the onboarding cases). No case was re-run.

| Adapter | Endpoint | Passed | NOOP | Undo | Input tokens (cached) | Output tokens |
| --- | --- | --- | --- | --- | --- | --- |
| `openai_compat` | `https://api.deepseek.com/` | **44/45** (97.8%) | 3/3 | 4/4 | 387,586 (277,376) | 11,919 |
| `anthropic` | `https://api.deepseek.com/anthropic` | **41/45** (91.1%) | 3/3 | 4/4 | 399,018 (376,192) | 11,260 |

| Category | `openai_compat` | `anthropic` |
| --- | --- | --- |
| inventory | 7/8 | 7/8 |
| shopping list | 5/5 | 5/5 |
| NOOP | 3/3 | 3/3 |
| undo | 4/4 | 4/4 |
| calendar | 7/7 | 7/7 |
| reminders | 4/4 | 4/4 |
| photos | 5/5 | 3/5 |
| onboarding | 9/9 | 8/9 |

On this one run the OpenAI-compatible endpoint is above the release bar and the
Anthropic-compatible endpoint is below it, so the bar ("on at least two providers") is **not met**.
One run is one sample per case: it does not show how often a case fails over many runs, and a
44/45 could be a 42 or a 45 next time.

Cost: the account balance, shown to the cent, read $4.90 before the run and $4.87 after it. The
nine live smoke turns earlier in the milestone had not moved it from $4.90, so part of the three
cents may be theirs.

Failures, from the assertion messages of this run (the cases were not re-run with tracing):

| Case | Endpoint | What happened |
| --- | --- | --- |
| batch finish with staple | `openai_compat` | Rows correct. The model replied "Eggs and bread are on the shopping list." where the case expects `ACK`; bread is not a staple and was not added, so the sentence is also wrong |
| batch finish with staple | `anthropic` | Bread was on the shopping list as well as eggs: the model added it itself. The same two behaviours were seen in milestone 1 |
| quiet hours changed later just by talking | `anthropic` | Ada said "don't message me after 8pm". Quiet hours were set to 20:00 to 07:00 for both adults: `remember` was called without `about`, which means everyone |
| receipt restocks what was bought | `anthropic` | Eggs and bread were logged from the receipt. Milk, which was on the list, was ticked off with `update_shopping_list` instead, so its restock has source `shopping`, not `receipt`. Stock and list ended correct |
| long receipt ignores totals and savings | `anthropic` | Seven items logged from the receipt, rice as a new item "basmati rice". The model then ticked "rice" off the list, a second restock of the same purchase under another name |

The two receipt failures are one weakness: a receipt's wording ("BASMATI RICE 5KG") does not
resolve to the household's own item ("rice"), and no deterministic rule can decide that for the
model ("coconut milk" is not "milk"). The `log_inventory` item description asks for the everyday
name; this run shows that it is not always followed. Duplicates can be merged on the item's
dashboard page.

## Earlier partial runs

These were runs of parts of the suite with fewer tools on offer, kept for comparison.

### Calendar and reminders (milestone 2)

One run of the 12 new cases (calendar 7, reminders 4, and the undo case "undo an appointment"),
6 Oct 2026, model `deepseek-flash` through both adapters.

| Adapter | Endpoint | Passed | Undo case | Input tokens (cached) | Output tokens |
| --- | --- | --- | --- | --- | --- |
| `openai_compat` | `https://api.deepseek.com/` | 12/12 | 1/1 | 97,712 (66,176) | 2,799 |
| `anthropic` | `https://api.deepseek.com/anthropic` | 12/12 | 1/1 | 101,556 (93,184) | 3,248 |

Cost: the account balance, shown to the cent, read $4.91 before the milestone's first model call
and $4.90 after this run and three live smoke turns (it still read $4.91 straight after the run and
changed a few minutes later). So the whole milestone cost about a cent.

One run is one sample per case. It shows the tools and prompts work together on this model; it
does not measure how often a case would fail over many runs.

### Inventory, list, NOOP and undo (milestone 1)

One full run of the 19 milestone 1 cases, 5 Oct 2026, model `deepseek-flash` (DeepSeek-V4.1-Flash)
through both adapters. Both were **below the release bar**. These cases were not re-run in
milestone 2, although the tool list the model sees has grown from five tools to nine.

| Adapter | Endpoint | Passed | NOOP | Undo | Input tokens (cached) | Output tokens |
| --- | --- | --- | --- | --- | --- | --- |
| `openai_compat` | `https://api.deepseek.com/` | 16/19 (84%) | 3/3 | 2/3 | 98,643 (65,920) | 4,723 |
| `anthropic` | `https://api.deepseek.com/anthropic` | 17/19 (89%) | 3/3 | 3/3 | 91,160 (82,432) | 3,921 |

Cost: the account balance, shown to the cent, read $4.93 before any model call in this milestone
and $4.91 after everything (adapter probes, two smoke turns, this run, and the traced re-runs
below). The full run itself is roughly a cent.

Failures, each traced by re-running the case with tool calls printed:

| Case | Adapters | What happened |
| --- | --- | --- |
| batch finish with staple | both | Rows were correct. The model replied with a sentence ("Eggs and bread added to the shopping list.") where the case expects `ACK`. In a traced re-run it also added the non-staple bread to the list itself |
| bought with a stated quantity | both | The model logged the restock, then also ticked milk off with `update_shopping_list`, which logged a second restock. **Fixed in code after the run**: ticking off an item already restocked in the same turn is now a no-op. The case passes on both adapters when re-run alone; the full suite was not re-run, so the table above predates the fix |
| undo a list change | `openai_compat` | `undo_last` ran and reverted the add. The model then re-added bleach on its own. 1 of 3 traced re-runs repeated this |

The two remaining failures are model behaviour under the spec's prompt, not tool or undo defects.
They need either prompt work or a stronger model before a release.
