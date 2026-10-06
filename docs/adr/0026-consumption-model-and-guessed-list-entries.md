# 0026 The consumption model, and guesses as rows on the shopping list

## Context

Spec section 10 defines the model in four sentences and two jobs, and section 9 says the list
shows "predicted items marked (probably)". It does not say what a predicted item is in the
database, what happens to one that is wrong, or how a cycle is measured when someone buys more
before running out.

## Decision

- **A cycle starts at the latest purchase** before the run-out. Restock, restock a week later,
  finished two days after that is a two-day cycle. The prediction is anchored at the last
  purchase, so the interval is measured from the same point; it errs towards asking early, and a
  prompt that comes after the milk has run out is worth nothing.
- **Every `discarded` ends a cycle**, as the spec lists it, including a partial one.
- **No prediction for an item that has run out** and not been bought since: the family already
  knows, and a staple is on the list already.
- **A guess is a `shopping_list_items` row with `reason = 'predicted'`**, written when the model
  is refreshed. The schema has the reason, the unique "one active row per item" index keeps a
  guess and a real entry from coexisting, and the dashboard's Bought and Remove buttons work on
  it unchanged.
- **Adding an item that is on the list as a guess promotes the row** to the real reason
  (`explicit`, `low`, `finished`), undoably. Without this, "yes, add the milk" would answer
  "already on the list" and the entry would stay a guess.
- **A guess that was taken off the list stays off until the item is bought again**, and a guess
  is retired a week after its predicted day if nobody said anything. Otherwise a wrong guess
  would be asked about every three days for ever.
- **`bought_all` ticks off only what was asked for.** "Got everything" at the shop should not
  record purchases of things that were only guessed.
- **The model is refreshed in three places**: the nightly job, and at the start of the 17:30
  prompt and the weekly digest, so both speak from the day's events and not from 03:00.
- **The nightly job runs late rather than not at all** (any time from 03:00 that day). It sends
  nothing, so there is no wrong moment for it. The 17:30 prompt keeps the four-hour limit that
  digests have: at 21:31 it is skipped.
- **Categories** are filled by one model call per household that must answer with a JSON object
  from a fixed list of eleven supermarket sections. An unknown section becomes `other`; an item
  left out of the answer is asked about again the next night; an unreadable answer or a failed
  call changes nothing. `LLM_FAST_MODEL` (default: `LLM_MODEL`) is used.

## Consequences

- The weekly digest's "probably running low this week" looks seven days ahead straight from the
  profiles; the list's guesses look two days ahead. They can name different items.
- Nothing is predicted until an item has run out twice and been bought again, so the first
  prompts come weeks after go-live.
- Guesses are system writes: they are not in Activity and cannot be undone as such. Promoting
  one can.
- The model call for categories is made outside any transaction, after the day's run is claimed.
  A failure waits for the next night.
- `get_shopping_list(include_predicted=true)`, the default, shows guesses to the model on every
  list request.
