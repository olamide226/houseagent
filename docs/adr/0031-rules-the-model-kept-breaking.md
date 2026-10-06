# 0031 Rules the model kept breaking are decided in code, and the tools say what code decided

## Context

Three mistakes came back in every run of the eval suite, and undo failed one sample in several
runs. Each was traced with the tool calls printed, five samples per case on both endpoints (230
case runs, 55 failed):

- **A non-staple on the list.** After "we're out of eggs and bread" the result said eggs were
  added and nothing about bread. In 7 of 10 runs the model then added bread itself; in the other 3
  it replied that both were on the list. A lone non-staple ("we've finished the ketchup") was left
  alone in 10 of 10: the model was repairing what looked like an omission.
- **A purchase under two names.** A receipt's "BASMATI RICE 5KG" was logged as a new item beside
  the household's rice in 10 of 10 runs, and in 8 of them the list entry for rice was then ticked
  off as a second restock.
- **Quiet hours for everyone.** "Don't message me after 8pm" and "I don't want any messages
  between 9pm and 6am" reached `remember` without `about` in 15 of 20 runs, and without `about`
  meant every adult ([ADR 0016](0016-settings-said-in-chat-go-through-remember.md)).
- **Undo.** Three different things: `undo_last` ran and the model then repeated the call it had
  just reverted; a correction ("not gone, just low") was logged as `low` with no undo, leaving a
  low item with a quantity of zero; and a model asked to "undo that" answered that there was
  nothing to undo, because its earlier reply ("Bleach is on the list.") was all it could see of
  what it had done.

The spec's principle is "LLM for language, code for rules", and its system prompt is verbatim.

## Decision

The static prompt is unchanged. Where code can decide, it does; where only language can, the
tool result puts the question to the model.

- **Quiet hours with nobody named are the speaker's.** `about: us` is every adult. This replaces
  the `quiet_hours` row of ADR 0016. A turn with no speaker and no name is an `ERROR:`.
- **Every `finished` and `low` line says what happened to the list**: added, already there, or
  "not added to shopping list: it is not a staple". Nothing is left for the model to infer.
- **A new name that ends with a name the household already has is not created by
  `log_inventory`.** The result says which item it found and the model answers: the same thing,
  logged again under the household's name, or a different product, logged again with
  `new_item: true`. Code cannot tell "semi skimmed milk" from "coconut milk"; the model can, and
  a wrong merge would tick milk off the list when no milk was bought. The line is an `ERROR:`,
  not `AMBIGUOUS:`, because the prompt tells the model to put an ambiguity to the family.
- **The call an undo just reverted is not run again in the same turn.** The turn's context
  remembers the tool and arguments of what was undone; the identical call answers `OK: not done
  again`.
- **`low` right after `finished` takes the `finished` back.** When the newest action on that
  stock row is a `log_inventory` whose only change was that `finished`, made in the last 24
  hours and not undone, it is undone before the `low` is applied, by the same code as
  `undo_last`. An action with other changes in it is left alone: undoing it would un-finish the
  others.
- **The brief ends with the speaker's last change that can still be undone**, as the tool
  reported it. Spec section 8.2 lists the brief's lines; this is one more.

Text the model reads that changed, each with its reason:

| Where | Change | Why |
| --- | --- | --- |
| `log_inventory` description | The list follows by itself; add what ran out only if asked, and do not offer to | Without it the explicit "not added" line was answered with "want it added?" in 9 of 12 runs |
| `log_inventory` description | A result of only `OK:`, `NEW:`, `NOTE:` lines is routine: reply `ACK`, or after a photo one line saying what was read | The spec's own case expects `ACK` and got a sentence in 9 of 10 runs; the photo clause keeps the read-back of a receipt |
| `log_inventory` argument `new_item` | New | The model's answer to the question above |
| `update_shopping_list` argument `bought` | Not for anything `log_inventory` recorded as bought | Receipts were logged and then ticked off again |
| `remember` argument `about` | `me`, `us`, and whose quiet hours are meant when it is left out | States the rule code now applies |
| `remember` argument `key` | Times as they were said (`7pm`), not `HH:MM` | "After 7pm" was sent as `20:00` in 5 of 10 runs; the parser already reads `7pm` |
| Setup step `rhythm` | Quiet hours `about us` | The setup question is about the household |

## Consequences

- A receipt line that ends with a household item's name costs one more model call.
- A name is only compared at its end, on whole words. "Milk chocolate" and "rice cake" are new
  items without a question; "egg fried rice" asks about rice. `update_shopping_list` and the
  `staples` setting still create items without asking.
- A non-staple that runs out is left off the list and the family is not told, as the spec's
  rule and its example case have it. It becomes a staple after two cycles, or when someone asks.
- Routine stock messages are answered with a reaction more often than before.
- The undo guard compares arguments exactly. A model that repeats the undone call with a
  different spelling is not stopped.
- Ticking an item off and then logging the same purchase, in that order, still leaves two
  restock events (stock ends right: the first has no quantity). The other order was already a
  no-op.
- Taking a `finished` back is not limited to the person who said it: "we're out" from one adult
  and "running low" from the other within a day is read as a correction.
