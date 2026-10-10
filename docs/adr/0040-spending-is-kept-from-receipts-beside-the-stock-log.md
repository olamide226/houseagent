# 0040 Spending is kept from receipts, beside the stock log, and added up by code

## Context

A receipt photo was already read: each line became a `restocked` event and the shopping list
ticked itself off. The prices on the same photo were thrown away. A family asked "what did we
spend this month, and on what?" could not be answered, and the spec had ruled price tracking out
of v1.

Three things made this more than two new columns. A sum a model works out in its head is wrong
often enough to be useless for money. A receipt is not its lines: there is a bag charge nobody
logs, a loyalty saving, a line the camera could not read. And every receipt this household sent
before today has no prices at all, which must not be shown as a month in which nothing was spent.

## Decision

- **Two tables beside the stock log, none of it in the stock log.** `receipts` holds the shop,
  the date, the printed total and the household's currency; `receipt_lines` the price of each line
  that was logged. `inventory_events` and `stock` are written exactly as before: the same events,
  in the same order, with the same list rules. Migration `0004`.
- **The model reads, code adds up.** `log_inventory` takes a `price` on each change and a
  `receipt` (shop, date, total) when its source is a receipt. Every amount a person is then told
  comes from `app/services/spending.py`: the `get_spending` tool, the Spending page and the monthly
  lines in the digest call the same functions. The tool's result is the sentence to relay.
- **Only what is printed is kept.** A line with no readable price keeps no line. A receipt with no
  readable total keeps none, and counts for its lines added up. A receipt with neither is not kept
  at all. A price that is only said in chat ("bread was £1.20") is not spending: the result says
  so. Code cannot see the photo, so this rests on the tool's wording and on eval cases with a torn
  receipt and a stained price.
- **A receipt counts for its printed total.** What the lines do not account for is shown, not
  hidden: "not listed line by line" where the total is above the lines (a bag, a line with no
  price), "discounts" where it is below. The two are worked out per receipt, so a saving at one
  shop does not cancel an unread line at another.
- **A receipt read without prices is said to have none.** Stock events from a receipt whose
  message has no `receipts` row are counted as "read without prices": every receipt from before
  this change, and any whose prices could not be read. The tool says what was spent "is not known"
  and tells the model not to say nothing was spent; the page shows "Prices not kept", never £0.
  The digest stays silent about a month with no priced receipt.
- **One receipt, one row.** A model that had to correct a name logs one receipt in two calls; the
  second adds to the first, found by the message it came in on, and a line it repeats is not kept
  twice. The second call often leaves the source out, which made the line a plain message and
  dropped its price (seen on the `anthropic` endpoint): a call that carries a price, whatever
  source it names, in a turn
  whose photo has already started a receipt, is taken as the rest of that receipt, for the stock
  event's source as for the price. This is the one place a stock event differs from before. The same receipt sent again on another day (same shop, date and total) is kept as a row
  that counts for nothing, so it is neither added twice nor taken for one read without prices.
- **The shop is filed under the name the household uses.** "TESCO STORES 2041" is their "Tesco"
  when a shop they named, or one on an earlier receipt, reads like it (the word similarity the
  shopping list already uses for shops). Otherwise the printed name, without the capitals.
- **A date after today counts as today**, and so does a date that could not be read. The result
  says so.
- **Kinds are the supermarket sections items already have**, filled by the nightly job. "Food" is
  seven of them. A line whose item has no section yet is "not sorted yet" and is said, not guessed.
- **A price change is the newest price against the last comparable one at the same shop**, for
  items the household always keeps in. The same pack is compared by what it cost; two sizes in the
  same unit by what one unit cost; anything else is not compared. A wrong "milk went up" is worse
  than none.
- **Correcting and removing are ordinary undoable actions.** `update_receipt` in chat and the
  Spending page both call `spending.correct` and `spending.remove`. Removing sets
  `status = 'removed'`: it changes the spending and never the stock. "Undo" straight after a
  receipt still reverts the stock and the receipt together, because both were written in one
  action.
- **The currency is the household's**, `households.currency`, default `GBP`, changed on Settings
  or by saying so (`remember`, key `currency`, [ADR 0016](0016-settings-said-in-chat-go-through-remember.md)).
  A receipt keeps the currency it was read in and is left out of sums in another, with a note.
- **The month goes out with the first weekly digest that runs in the next month**, claimed in
  `job_runs` as `monthly_spending` so it is said once, and still said if the first Sunday was
  missed.
- **The guide follows the installation.** Where photos cannot be read there are no receipts, so
  the guide does not offer spending there and says the page has nothing to show yet.

## Consequences

- Two more tools (eighteen with meal ideas, household jobs and letters), one more page (twelve in the menu), and three more optional fields on
  `log_inventory`. Every model step reads about 1,540 more tokens, cached (6,537 against 4,998 on
  a one-step turn). The eval suite was run before and after on both endpoints: nothing that passed
  before failed after ([evals.md](../evals.md#previous-full-run-spending-from-receipts)).
- The total is only as complete as the receipts sent. Cash at a market with no receipt, a
  takeaway, a bill: none of it is here. The page and the answers say "from your receipts".
- A receipt from abroad is counted as if it were in the household's currency. It can be removed.
- Price changes need the same item bought twice at the same shop with a receipt, so the section
  stays empty for the first weeks.
- "On food" is exact only once the nightly job has sorted the items; until then the answer says
  how much is on lines not sorted yet.
- A receipt whose every line failed to resolve and which shows no total is not kept, and the
  model's second call may leave out the shop. It is then kept without one and can be corrected.
- Two shops whose names read alike ("Tesco Extra", "Tesco Express") are one shop in the sums.
- Merging a duplicate item moves its receipt lines to the kept item, like its events.
- The export has both tables. Deleting a household deletes its receipts.
