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

31 cases: inventory (8), shopping list (5), NOOP (3), undo (4), calendar (7), reminders (4). The
spec's target is 40 cases including photos, which arrive with their milestone. Release bar: 95%
overall and 100% on the NOOP and undo cases, on at least two providers.

## Latest results

The suite has not been run as a whole since milestone 2 added its cases. The two tables below are
separate runs of separate parts, so there is **no current full-suite score**, and the milestone 1
part was below the release bar when last measured.

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
