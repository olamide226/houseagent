# Agent evals

Agent quality is judged by database state, never by wording. Each case in `tests/evals/*.yaml`
seeds a household, sends its turns through `simulate_turn()` (the service the dashboard Playground
uses), and asserts on rows.

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

Item names are normalised the way the resolver does, so `eggs` and `egg` are the same item.

## Suite

19 cases: inventory (8), shopping list (5), NOOP (3), undo (3). The spec's target is 40 cases with
calendar, reminders and photos; those arrive with their milestones. Release bar: 95% overall and
100% on the NOOP and undo cases, on at least two providers.

## Latest results

One full run, 5 Oct 2026, model `deepseek-flash` (DeepSeek-V4.1-Flash) through both adapters.
Both are **below the release bar**.

| Adapter | Endpoint | Passed | NOOP | Undo | Input tokens (cached) | Output tokens |
| --- | --- | --- | --- | --- | --- | --- |
| `openai_compat` | `https://api.deepseek.com/` | 16/19 (84%) | 3/3 | 2/3 | 98,643 (65,920) | 4,723 |
| `anthropic` | `https://api.deepseek.com/anthropic` | 17/19 (89%) | 3/3 | 3/3 | 91,160 (82,432) | 3,921 |

Cost: the account balance, shown to the cent, read $4.93 before the run and $4.92 after it and the
follow-up checks below, so the run cost about a cent.

Failures, each traced by re-running the case with tool calls printed:

| Case | Adapters | What happened |
| --- | --- | --- |
| batch finish with staple | both | Rows were correct. The model replied with a sentence ("Eggs and bread added to the shopping list.") where the case expects `ACK`. In a traced re-run it also added the non-staple bread to the list itself |
| bought with a stated quantity | both | The model logged the restock, then also ticked milk off with `update_shopping_list`, which logged a second restock. **Fixed in code after the run**: ticking off an item already restocked in the same turn is now a no-op. The case passes on both adapters when re-run alone; the full suite was not re-run, so the table above predates the fix |
| undo a list change | `openai_compat` | `undo_last` ran and reverted the add. The model then re-added bleach on its own. 1 of 3 traced re-runs repeated this |

The two remaining failures are model behaviour under the spec's prompt, not tool or undo defects.
They need either prompt work or a stronger model before a release.
