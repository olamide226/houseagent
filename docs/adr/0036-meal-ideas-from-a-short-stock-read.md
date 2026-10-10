# 0036 Meal ideas: the model proposes, from a short read of what there is to cook with

## Context

The assistant knew what was in the house and what goes out of date soon, and did nothing with it
but answer "do we have rice?". "What can we cook tonight?" is asked every day, and answering it
from the stock is also what gets the chicken used before Wednesday.

Three things had to be decided: where the meals come from, how the stock reaches the model, and
what happens when someone picks a meal.

**The stock.** The brief carries what is low and what goes out of date within 3 days, not what
is in the house: the whole pantry on every turn would be paid for on turns about the dentist.
`query_inventory` with no filter returns it, one line a row (`rice: in stock, 2 (store)`), with
what has run out and the bleach as well. Measured with the provider's own token count on made-up
pantries:

| Items in the house | One line a row (`query_inventory`) | `meal_ideas` |
| --- | --- | --- |
| 40 | 454 tokens | 191 |
| 120 | 1,463 | 631 |
| 300 | 3,699 | 637 |

**The rules for answering** (how many meals, what to say is missing, allergies, the offer) can
sit in the tool's description or come back with the food. A description is read on every model
step of every turn. The first version had them there and one model step grew by 338 tokens
(4,898 to 5,236); with the rules in the result it grew by 177.

## Decision

**One read tool, `meal_ideas`, with no arguments. The model proposes the meals; code decides what
it is shown and in what order.** There is no recipe list and no outside recipe service.

- **Code does what is a rule.** What has run out is left out, and so is what is not food (the
  nightly sorting's `household`, `toiletries` and `baby` sections). What has a date within 7 days
  comes first, soonest first, under `Use first`; what is past its date is listed apart. "Today"
  is the household's own date when the message arrived.
- **A long pantry stays short.** An item is a word or two on its place's line, not a line of its
  own. Past 120 items the least recently recorded are counted and not named; what has a date is
  always shown.
- **Everything remembered comes with it**, on one line. The brief shows the first 25 facts, and an
  allergy must not be the 26th. It is also beside the food at the moment the meals are chosen.
- **How to answer is the result's last line**, not the description. The description keeps what
  must be read on a turn where the tool is not called: when to call it, and that picking a meal
  means adding what it still needs.
- **Picking a meal is `update_shopping_list`.** The suggestion is in the thread history, so "the
  first one" is answered from it. Nothing new is stored, there is no migration, and the pick is
  undone like any other change to the list.
- **A meal's missing things are the model's word.** Code does not check them against the stock.
  It can see that "chicken" and "chicken thigh" are related; it cannot say that one is the other
  ([ADR 0031](0031-rules-the-model-kept-breaking.md) met the same thing with receipts). Asking
  for something that is already on the list changes nothing, as before.
- **The Today page gets one line of words**, under "Use soon", saying how to ask. It draws no
  ideas and calls no model. Ideas on the page would need them stored, or a model call from a
  page.

## Consequences

- Every model step reads about 177 more tokens (the description, the guide's line, the schema),
  in the cached part of the prompt. A turn about meals reads the food and about 150 tokens of
  rules once, in its second step.
- A meal question is two model steps: the read, then the answer. A pick is two: the add, then
  the confirmation.
- Whether a suggested meal really needs nothing more is not checked by anything. In the runs
  read for this change one answer called a bolognese complete with no tomatoes in the house.
- Remembered facts are free text. "Ada: diet=vegetarian" is kept to because the model reads it,
  not because code knows what a vegetarian eats. The eval cases check that no suggested meal
  names the thing ruled out; they cannot check a dish for what is in it.
- Food past its date is named and not cooked with. The date is whatever was recorded from the
  pack; the assistant does not know a use-by date from a best-before one.
- An item not yet sorted into a section counts as food until the nightly sorting has run, so
  bleach bought today can be in tonight's list of what there is.
- A pick made more than 20 messages or 48 hours after the suggestion is no longer in the thread
  history, and the model has nothing to read the missing things from.
- A question about dinner in the family group is answered there when the assistant is addressed
  by name, and "I'm doing jollof tonight" is left alone: both are eval cases. "What shall we have
  for dinner tonight?" with no name was answered too, in the two runs that were read; no case
  pins that either way.
- Under the Letta runtime the tool is registered like the others. That path was not run.
