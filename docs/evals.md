# Agent evals

Agent quality is judged by database state, not by wording. Each case in `tests/evals/*.yaml`
seeds a household, sends its turns through `simulate_turn()` (the service the dashboard's Practice chat
uses), and asserts on rows. The one exception is `product.yaml`, the questions about the product
itself, where the answer is the reply: those cases assert that nothing was written and check the
reply only for a word that has to be in it, and for words that must not be.

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
Results, with token totals, are written to `tests/evals/.results/<provider>.json` (git-ignored),
which is also where the dashboard's System page reads the last result from.
Add `-k "calendar or reminders"` to run part of the suite.

One run is one sample per case, and several cases fail one time in five. `EVAL_SAMPLES=5` runs
every selected case five times (the result file then has one entry per run), and `EVAL_TRACE=1`
with `pytest -s` prints each turn's tool calls, their results and the reply, which is how a
failure is told apart from the model answering differently:

```sh
RUN_EVALS=1 EVAL_SAMPLES=5 EVAL_TRACE=1 ... uv run pytest tests/evals -s -k "undo: or receipt"
```

To run them through a subscription provider ([llm.md](llm.md#subscription-providers)), name its
model. The CLI must be installed and signed in on this machine; no key is used, and the run draws
on that subscription's allowance:

```sh
RUN_EVALS=1 EVAL_CLAUDE_CODE_MODEL=haiku uv run pytest tests/evals -k claude_code
RUN_EVALS=1 EVAL_CODEX_CLI_MODEL=gpt-5.6-luna uv run pytest tests/evals -k codex_cli
```

To run the same cases through the optional Letta runtime instead, start a Letta server and set
`EVAL_RUNTIME=letta`, `EVAL_LETTA_BASE_URL` (default `http://127.0.0.1:8283`) and
`EVAL_LETTA_MODEL` (the model's handle on that server). The test process then serves the tool
bridge itself on `EVAL_BRIDGE_PORT` (8011); `EVAL_BRIDGE_URL` is how the Letta server reaches
it, by default `http://host.docker.internal:8011`. The result is written as `letta.json`.
[The Letta comparison](#the-letta-comparison) has the exact commands that were used.

## Adding a case

```yaml
- name: batch finish with staple
  seed:
    items: [{name: egg, staple: true, stock: {fridge: 6}}, {name: bread, stock: {store: 1}}]
    list: []                      # item names already on the shopping list
  turns: [{from: Ola, text: "we're out of eggs and bread"}]   # from: Ola or Ada; scope: dm (default) or group
  # a turn may also carry photos: [receipt_supermarket.png], files in tests/evals/fixtures/;
  # files: [autumn_dates.pdf], a PDF from there, read as the pipeline reads one;
  # and forwarded: {from: Coach Bisi, sent: "2026-10-04 20:00"}, what a channel says of a forward
  expect:
    events: [{item: egg, type: finished}, {item: bread, type: finished}]
    shopping_list_active: [egg]
    reply: ACK
```

| `expect` key | Asserts |
| --- | --- |
| `events` | Exactly these inventory events (undo markers excluded). `type` may be a list of accepted types; `quantity` and `location` are checked when given |
| `shopping_list_active` | Exactly these items are on the list, guesses included |
| `shopping_list_asked_for` | Exactly these items are on the list as real entries, not as "probably" guesses |
| `stock` | Per item: `status`, and `qty` and `location` when given |
| `writes: 0` | No events, no logged actions, and the list is unchanged from the seed |
| `actions` | How many changes were logged, for a case whose seed already holds events |
| `receipts` | Exactly these kept receipts, in any order. Each may give `shop_contains`, `total` (`null`: no total was kept), `day`, `prices` (a line whose name contains the key has that price), `no_price` (nothing with that in its name has a line) and `lines_max` |
| `receipts_removed` | How many receipts are out of the spending |
| `reply` | The last turn's outcome: `ACK`, `NOOP`, or `text` for any other reply |
| `reply_mentions` | Each of these texts is in the last reply: a reply that reads the list back has to name what is on it, and an answer about the product has to name the word to send or the page |
| `reply_says_any` | At least one of these is in the last reply as a whole word, so `not` is not found in "note". For answers that have to say no |
| `reply_avoids` | None of these texts is in the last reply |
| `called` | The last turn called each of these tools. For a question that only reads, where no row can show what the answer was built on |
| `meals_avoid` | No suggested meal, a numbered or bulleted line of the last reply, has any of these texts, and there is at least one such line. A remark beside the meals ("the chicken is not for Ada") may name them, and so may a meal's own line when it rules the thing out ("eggy bread (no peanuts)") |
| `plain: true` | The last reply has no word from `TECHNICAL` in `tests/helpers.py` (api, url, token, database, webhook, endpoint, json, backend, server, config), no link, and no name_with_underscores such as a tool or a setting |
| `calendar` | Exactly these active events, in any order. Each may give `title_contains`, `participants`, `local_start` ("Wed 7 Oct 11:00"), `location_contains`, `rrule_contains`, `repeats: false`, `exdates` |
| `calendar_cancelled` | How many events are cancelled |
| `reminders_scheduled` | How many reminder rows are scheduled, for events and standalone |
| `reminders` | Exactly these scheduled standalone reminders, household jobs not among them: `text_contains`, `local_fire` (one time, or a list of accepted times), `target`, `member`, `urgency`, `rrule_contains`, `repeats: false` |
| `jobs` | Exactly these open household jobs, in any order. Each may give `title_contains`, `kind` (`job` or `date`), `owner` (`null` for everyone), `local_due` ("Tue 13 Oct 19:00"), `due_day` ("Sun 14 Mar", when any time of day will do), `all_day`, `rrule_contains`, `repeats: false`, `waiting` (asked about and not ticked off), `done_by`, `local_fire` (when it is next said; one time or a list of accepted times) |
| `jobs_done`, `jobs_stopped` | How many jobs are ticked off for good; how many were stopped |
| `events_include` | Photos: an event whose item name contains `item_contains`, of one of the `type`s, exists. A model may call the item "milk" or "semi skimmed milk" |
| `events_all` | Every event has this `type`, `source` or `location` (a value or a list of accepted values) |
| `events_max` | No more events than this, so totals, savings and invented items fail the case |
| `new_items_max` | No more items than this were created beyond the seeded ones, so a purchase logged under a new name beside the household's own item fails the case |
| `members_added` | Exactly these people beyond Ola and Ada, with their roles |
| `onboarding_done`, `onboarding_step` | Steps that must be done; the step setup is now on (`null` when complete) |
| `staples`, `shops`, `facts`, `facts_mention` | Items flagged as staples; shop names containing each text; a fact by key whose value contains the text; any fact value containing the text |
| `brief`, `quiet` | The brief time (`"07:00"`); quiet hours per adult (`"22:00-06:30"`) |
| `invites_sent` | How many invite messages were queued, each to the person who asked |
| `shops_offers` | Exactly these adults were offered the list on arriving at a shop, and nobody was given a link unasked |

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

A case about something the assistant said first seeds that message, and any guessed entries:

```yaml
- name: only one of the guessed items is wanted
  seed:
    items: [{name: milk, stock: {fridge: 1}}, {name: bread, stock: {store: 1}}]
    guesses: [milk, bread]        # on the list as "probably", as the consumption model leaves them
    said: [{to: Ada, text: "Probably running low: milk, bread. Add to the list?"}]   # five minutes ago, in her DM
  turns: [{from: Ada, text: "just the milk, we've got plenty of bread"}]
  expect:
    shopping_list_asked_for: [milk]
```

A case about meals can give stock a date and the household something remembered:

```yaml
- name: a vegetarian is not offered the chicken that needs using
  seed:
    facts: [{key: diet, value: vegetarian, about: Ada}]     # about: a member; without it, the whole household
    items:
      - {name: chicken thigh, stock: {fridge: 1}, expires: "2026-10-06"}   # the date on the pack
      - {name: egg, stock: {fridge: 6}}
  turns: [{from: Ola, text: "what can I cook for Ada tonight?"}]
  expect: {writes: 0, plain: true, called: [meal_ideas], meals_avoid: [chicken]}
```

A case about the product can say what the installation has switched on. Without `setup` there is
Telegram only, photos are read, voice notes are not, and the shared Shortcut is not set:

```yaml
- name: photos cannot be read where they are not switched on
  seed: {setup: {photos: false}}     # also chat_apps: [telegram, whatsapp], voice_notes, shop_shortcut
  turns: [{from: Ola, text: "can you read photos?"}]
  expect: {writes: 0, plain: true, reply_says_any: ["can't", cannot, not, unable], reply_avoids: ["yes"]}
```

A case about a household job can seed jobs, and say that one has already had its first nudge:

```yaml
- name: done answers the nudge and next week's is set
  now: "2026-10-06 19:10"
  seed:
    jobs:                              # due: a day and a time, or a day alone; owner: a name, or everyone
      - {title: Put the bins out, due: "2026-10-06 19:00", rrule: "FREQ=WEEKLY;BYDAY=TU", owner: Ola, asked: true}
  turns: [{from: Ola, text: "done"}]
  expect:
    jobs: [{title_contains: bin, owner: Ola, local_due: "Tue 13 Oct 19:00", waiting: false, done_by: Ola}]
    reply: ACK
```

A seeded job is set up through the service the day before it is first due. With `asked: true` the
worker's own code has sent its first nudge, five minutes before the case starts, into the chat it
would go to: the owner's, or the family group for a job that is everyone's.

A spending case seeds receipts as a photo leaves them, with the section each item is sorted into,
and can seed receipts that were read before prices were kept:

```yaml
- name: what was spent on food
  seed:
    receipts:
      - {shop: Hilltop Grocers, day: "2026-10-04", total: 8.95, lines: [[butter, 2.40], [coconut milk, 1.10]]}
      - {shop: Freshway Supermarket, day: "2026-10-03", total: 5.80, lines: [[milk, 1.65, 2, pints], [bread, 1.20]]}
    sections: {butter: dairy and eggs, milk: dairy and eggs, bread: bakery}   # a line is [item, price, quantity, unit]
    unpriced_receipts: ["2026-10-02"]      # the day of a receipt whose prices were never kept
  turns: [{from: Ola, text: "how much of this month's spending was food?"}]
  expect: {actions: 0, plain: true, reply_mentions: ["5.25"]}
```

The amount in `reply_mentions` is the one code adds up, so a model that does its own sum, or
answers from the brief, fails the case.

Seeded events go through the calendar service, so they have their reminders, but leave no undo
record. The spec's example uses the key `events` for calendar events; that key already meant
inventory events here, so calendar expectations are under `calendar`.

## Suite

131 cases: inventory (12), shopping list (5), NOOP (3), undo (7), calendar (8), reminders (5),
photos (6: four receipts, two fridge or freezer photos), onboarding, family and settings (13),
presence (4: answers to the low-stock prompt and the "out and about" offer), product (22:
what the assistant says about itself, [ADR 0034](adr/0034-the-assistant-is-told-what-is-built.md)),
meals (10: what to cook, [ADR 0036](adr/0036-meal-ideas-from-a-short-stock-read.md)),
household jobs (14, [ADR 0037](adr/0037-a-household-job-is-a-reminder-that-is-to-be-done.md)),
letters (9: what someone sends on,
[ADR 0039](adr/0039-a-letter-is-one-call-and-a-day-with-no-time-is-a-reminder.md)),
and spending (13, [ADR 0040](adr/0040-spending-is-kept-from-receipts-beside-the-stock-log.md)).
The spec's target is 40.

The meal cases ask what to cook tonight, for the week, with a named ingredient, for a vegetarian
with chicken that needs using, for a child with a peanut allergy and peanut butter in the
cupboard, with nothing recorded, and by name in the family group. Each asserts that the food was
read (`called`), that nothing was written, and plain words. Two start from a suggestion already
made: picking the meal that needs two things puts those two on the list, and picking the one
that needs nothing writes nothing. The last is someone saying in the group what they are cooking,
which is not a question and expects `NOOP`.

The job cases set one up four ways (the bins every Tuesday night with an owner, the MOT on a
day, a prescription every 28 days for the person it is named for, a birthday), answer a nudge
("done", "later", "done" in the family group, the right one of two that are waiting), tick off a
job before its day, hand one to someone else, skip a week, stop one, ask whose job it is, and
leave alone a nudge one adult gives the other in the group. With them came one reminder case
("later" in answer to a plain reminder sets it again two hours on) and one product case (where
to see the jobs). The reminder cases now fail if a reminder is set up as a job: "remind us
every Sunday at 6pm to put the bins out" has to stay a reminder.

The letters cases send what a family sends: a photo of a school letter with a trip, a slip due
back and a day off; a photo of a party invite; an appointment message forwarded from a clinic; a
coach's message forwarded the next day, in which "tomorrow" is already today; pasted words with
one date that has passed; and a PDF of term dates in which one weekday does not match its date.
They assert the calendar and reminder rows, that "undo that" leaves none of them, that the
doubtful date is asked about and added after the answer, and that a forwarded joke in the family
group gets no reply. The last product case asks whether letters can be sent on.

The spending cases: three receipt photos keep their shop, their total and each line's price (the
long one keeps the printed total and no price for a saving; a torn one with a stained price keeps
the three prices that can be read, no total and nothing for the stained line); undo after a
receipt takes the prices back with the stock; a price only said in chat is not spending; then
seven questions answered from seeded receipts (this month, one shop, food, last month, whether
milk has gone up, a month whose receipts have no prices, which must not be answered with a
zero), a total corrected and a receipt taken out. The amount each reply must name is the one code
adds up.

The product cases ask what a family member asks: "what can you do?", "how do I see the list on my
laptop?", "how do I add my wife?", "how does the shop thing work?", "can you read photos?" where
photos are on and where they are not, "switch me to WhatsApp" where it is set up and where it is
not, a feature that does not exist (ordering the shopping online), a how-to question in the family
group, and where the jobs and the spending are shown. Each asserts no writes, plain words, and the
word or page that has to be named.

Nine more are about an adult's own calendar
([ADR 0038](adr/0038-a-connected-calendar-is-a-second-copy-kept-in-step.md),
[ADR 0041](adr/0041-apple-calendar-is-caldav-with-an-app-specific-password.md)): how to get plans
into Google Calendar where that is switched on (the Calendar page and its Connect button) and
where it is not, "is my Google calendar connected?" from someone whose calendar is, and "why
have my plans stopped showing up there?" from someone whose connection stopped. For Apple:
whether an iPhone's calendar can be connected, where it is switched on (yes: the Calendar page
and a password) and where it is not (no, and neither the button nor an app-specific password is
named); the same two questions about a connection that is working and one that stopped; and
someone who sends their Apple email and a password in chat, where nothing may be written (which
is also what says it was not remembered), the password may not be said back, the reply may not
claim to have connected anything, and the Calendar page has to be named. `seed.own_calendar`
gives an adult a connection of either kind in a named state. One calendar case goes with them: someone
connected books the dentist and then asks for it to be put in their Google calendar "as well",
and there must still be one event.

Eleven of them are variants written for the pass on repeat mistakes
([ADR 0031](adr/0031-rules-the-model-kept-breaking.md)): the same mistake asked for with other
items, other wording and another shop, so that a fix is not judged only on the case it was
written against. They are the last cases in `inventory.yaml`, `photos.yaml`, `onboarding.yaml`
and `undo.yaml`. Two of them check the other direction: a finished non-staple that the person
asks to have listed is listed, and coconut milk on a receipt does not tick off the milk. Release bar: 95% overall and 100% on the NOOP and undo cases, on at
least two providers.

The photo fixtures in `tests/evals/fixtures/` are drawn by `make_fixtures.py` in that folder
(`uv run --with pillow python tests/evals/fixtures/make_fixtures.py`), which also writes the one
PDF. None is a real photo or a real letter: the school, the people and the addresses are made
up. A photo case reads its image through a read-only `MediaStore` over that folder, so the image
takes the same path into the model as a stored Telegram photo. A PDF case reads its file with the
pipeline's own reader. What no case exercises is the way in from a chat app: the webhook, the
download and the forward mark are covered by unit tests with stand-in payloads.

## Latest results

One run of the whole suite with the loop runtime, 10 Oct 2026, with Apple Calendar
([ADR 0041](adr/0041-apple-calendar-is-caldav-with-an-app-specific-password.md)) on top of
spending, model `deepseek-flash` through both adapters: the 131 cases, 889 seconds. "Before" is
`main` as this change was put on top of it, the "after" of spending
[below](#previous-full-run-spending-from-receipts): the 127 cases it had. `main` was not run again.

| Adapter | Before | After | NOOP | Undo | Input tokens after (cached) | Output tokens after |
| --- | --- | --- | --- | --- | --- | --- |
| `openai_compat` | 127/127 | **131/131** | 3/3 | 7/7 | 2,612,211 (2,140,160) | 36,747 |
| `anthropic` | 127/127 | **130/131** | 3/3 | 7/7 | 2,611,897 (2,540,539) | 37,100 |

| Category | `openai_compat` before | after | `anthropic` before | after |
| --- | --- | --- | --- | --- |
| inventory | 12/12 | 12/12 | 12/12 | 12/12 |
| shopping list | 5/5 | 5/5 | 5/5 | 5/5 |
| NOOP | 3/3 | 3/3 | 3/3 | 3/3 |
| undo | 7/7 | 7/7 | 7/7 | 7/7 |
| calendar | 8/8 | 8/8 | 8/8 | 8/8 |
| reminders | 5/5 | 5/5 | 5/5 | 4/5 |
| photos | 6/6 | 6/6 | 6/6 | 6/6 |
| onboarding | 13/13 | 13/13 | 13/13 | 13/13 |
| presence | 4/4 | 4/4 | 4/4 | 4/4 |
| product | 18/18 | 22/22 | 18/18 | 22/22 |
| meals | 10/10 | 10/10 | 10/10 | 10/10 |
| household jobs | 14/14 | 14/14 | 14/14 | 14/14 |
| letters | 9/9 | 9/9 | 9/9 | 9/9 |
| spending | 13/13 | 13/13 | 13/13 | 13/13 |

The release bar (95% overall and 100% on NOOP and undo, on at least two providers) is met on both
endpoints. It is one sample per case, and the two endpoints are one model on two wire formats.

**The one failure.** "Reminder in two hours" on `anthropic`: Ada asks at 12:00 to be reminded to
ring her mum in two hours, and the reminder was set for 14:30, not 14:00. The case has nothing to
do with calendars, but this change does make the guide longer on every turn, so it was run ten
more times on each endpoint: 20 of 20 passed. That is one wrong time in 21 runs on that
endpoint. Whether `main` does the same was not measured.

**What changed in the suite.** Four product cases are new and one was rewritten. Before the full
run the ten cases that touch an adult's own calendar (nine product cases and one calendar case)
were run three times each on both endpoints: 57 of 60 passed. The three that failed were one case, "Apple Calendar gets the link and no promise"
where neither calendar is switched on: the replies said, correctly, that it cannot be connected
here and that whoever set this up has to switch it on, and did not go on to mention the one-way
link, which the case required. A change made on the phone would not come back through that link
anyway, so the case was asking for more than the question does. It now requires the "no", and
that neither the button nor an app-specific password is named. It passed on both endpoints in
the full run, once each.

**What it costs.** With both calendars switched on the guide is 104 words longer than on `main`
(1,545 against 1,441), all of it in the cached part of the prompt. With neither switched on a
one-step turn reads 9,717 input tokens, against 9,714 before. The full run, the 60 runs before
it and the 40 after it used about 6 million input tokens, most of them cached, and under 90,000
output tokens. The account balance was not read before and after.

What these cases do not show: anything about the sync itself. They ask the assistant how to
connect a calendar and how one stands. The sync is covered by the unit and contract tests, and
has never met iCloud.

## Previous full run (spending from receipts)

One run of the whole suite with the loop runtime, 10 Oct 2026, with spending from receipts
([ADR 0040](adr/0040-spending-is-kept-from-receipts-beside-the-stock-log.md)) on top of meal
ideas, household jobs, calendar sync and letters, model `deepseek-flash` through both adapters:
the 127 cases, 896 seconds. "Before" is `main` as this change was put on top of it, the "after" of
letters [below](#previous-full-run-letters-invites-and-forwards): the 113 cases it had.

| Adapter | Before | After | NOOP | Undo | Input tokens after (cached) | Output tokens after |
| --- | --- | --- | --- | --- | --- | --- |
| `openai_compat` | 113/113 | **127/127** | 3/3 | 7/7 | 2,571,173 (2,044,672) | 37,119 |
| `anthropic` | 112/113 | **127/127** | 3/3 | 7/7 | 2,566,921 (2,490,609) | 39,523 |

| Category | `openai_compat` before | after | `anthropic` before | after |
| --- | --- | --- | --- | --- |
| inventory | 12/12 | 12/12 | 11/12 | 12/12 |
| shopping list | 5/5 | 5/5 | 5/5 | 5/5 |
| NOOP | 3/3 | 3/3 | 3/3 | 3/3 |
| undo | 7/7 | 7/7 | 7/7 | 7/7 |
| calendar | 8/8 | 8/8 | 8/8 | 8/8 |
| reminders | 5/5 | 5/5 | 5/5 | 5/5 |
| photos | 6/6 | 6/6 | 6/6 | 6/6 |
| onboarding | 13/13 | 13/13 | 13/13 | 13/13 |
| presence | 4/4 | 4/4 | 4/4 | 4/4 |
| product | 17/17 | 18/18 | 17/17 | 18/18 |
| meals | 10/10 | 10/10 | 10/10 | 10/10 |
| household jobs | 14/14 | 14/14 | 14/14 | 14/14 |
| letters | 9/9 | 9/9 | 9/9 | 9/9 |
| spending (new) | not run | 13/13 | not run | 13/13 |

The release bar (95% overall and 100% on NOOP and undo, on at least two providers) is met on both
endpoints. It is one sample per case, and the two endpoints are one model on two wire formats.
Nothing failed in this run. That is one run: the run before it lost three cases, and two of them
are cases that fail some of the time with or without this change.

**One rebase earlier**, on calendar sync without letters, the suite had 117 cases: 116/117 on
`openai_compat` and 115/117 on `anthropic`, 797 seconds. That run kept no trace of the tool calls.

| Case | Endpoint | What is known | What was done |
| --- | --- | --- | --- |
| whose job is whose is on the Jobs page | `anthropic` | The case that failed in the 111-case run, below | Nothing |
| bought one thing off the list | `anthropic` | The case that failed on `main` that morning and in the runs of 7 Oct, where the eggs were logged as `added` | Nothing |
| a long receipt keeps the printed total and no price for a saving | `openai_compat` | The receipt was kept with its total and seven line prices. The rice had none: it is the line the model has to log a second time, under the household's own name for it | Below |

The long receipt was then run 28 more times on each endpoint with the trace on: 56 of 56 passed.
Every second call carried the rice's price, 54 with the source given as a receipt and 2 without.
So what the second call sent in the run that failed is not known. It is the one failure in the
70 runs or more that this case has had. Reading the code for that call found one way a price could be lost: the second
call was taken as the rest of the receipt when it left the source out, but not when it called the
source a photo. That is closed, and a unit test holds it. It is not shown to be what happened.
The spending, photo, inventory and undo cases were run again after it: 41 of 41 on both endpoints.

**Two rebases earlier**, on meal ideas and household jobs without calendar sync: 111 cases, 729
seconds. "Before" there is the run of the 97 cases [below](#previous-full-run-household-jobs).

| Adapter | Before | After | NOOP | Undo | Input tokens after (cached) | Output tokens after |
| --- | --- | --- | --- | --- | --- | --- |
| `openai_compat` | 97/97 | **111/111** | 3/3 | 7/7 | 1,939,130 (1,535,744) | 30,476 |
| `anthropic` | 97/97 | **110/111** | 3/3 | 7/7 | 1,956,219 (1,907,837) | 28,276 |

| Category | `openai_compat` | `anthropic` |
| --- | --- | --- |
| inventory | 12/12 | 12/12 |
| shopping list | 5/5 | 5/5 |
| NOOP | 3/3 | 3/3 |
| undo | 7/7 | 7/7 |
| calendar | 7/7 | 7/7 |
| reminders | 5/5 | 5/5 |
| photos | 6/6 | 6/6 |
| onboarding | 13/13 | 13/13 |
| presence | 4/4 | 4/4 |
| product | 12/12 | 11/12 |
| meals | 10/10 | 10/10 |
| household jobs | 14/14 | 14/14 |
| spending (new) | 13/13 | 13/13 |

The release bar (95% overall and 100% on NOOP and undo, on at least two providers) is met on both
endpoints. It is one sample per case, and the two endpoints are one model on two wire formats.

**The one failure of that run is not from this change.** "Whose job is whose is on the Jobs page" asks where
the household jobs can be seen and requires both `Jobs` and `dashboard` in the reply. The reply
was "Yes. Open the Jobs page — it lists what's coming up, whose each one is, and lets you tick one
off, skip it or stop it." Eight samples on each endpoint gave an answer without the word
`dashboard` 6 times in 16 on the code before this change and 7 times in 16 after it. The case was
left as it is: it belongs to household jobs, and whether an answer that names the page must also
say how to open it is that case's call.

**Before the first two rebases** the same change was run on the 71 cases it started from, the same day:

| Adapter | The 71 cases before | With spending: 85 cases, first run | Second run |
| --- | --- | --- | --- |
| `openai_compat` | 71/71 | 85/85 | 85/85 |
| `anthropic` | 70/71 | 83/85 | 85/85 |

The failure before was "bought one thing off the list" (the eggs logged as `added`). The two
failures of the first run were followed up, and the second run is the code after that:

- "A receipt keeps its shop, its total and the price of each line". The model logged "free range
  eggs", was told the household has eggs, and logged them again in a second call that left the
  source out. The eggs became a plain message and their price was not kept. Code now takes a call
  that carries a price, in a turn whose photo has already started a receipt, as the rest of that
  receipt. A unit test holds it.
- "Seeing the list on a laptop is the word dashboard". The reply was right ("Message me the word
  dashboard on its own and you'll get a private link...") and the case only accepted send, type,
  text, write or say. Six samples on each endpoint gave that sentence once in twelve on the code
  as it was and not at all after, so it is the case and not the change: `message` was added to
  the words it accepts.

**What it costs.** One model step reads 9,714 input tokens where `main` reads 8,174 (the three
one-step NOOP cases; the figure for `main` is the one recorded with letters, below): 1,540 more,
for two more tools, three more fields on `log_inventory` and five more lines of guide. Before the
rebases the same three cases gave 8,428 against 6,889, which is 1,539. All of it is in the cached
part of the prompt. The account balance read $3.86 before the first run for this change and $2.04
after the last, but other work was drawing on the same key all day, so $1.82 is an upper bound,
not the cost: the whole suite six times at 71 to 127 cases (a seventh was stopped half way when
`main` moved), and about 270 runs of single cases and parts of the suite.

What the spending cases do not show: a real receipt. All three are drawn, with eight lines at
most, printed straight and in one currency.

## Previous full run (letters, invites and forwards)

One run of the whole suite with the loop runtime, 10 Oct 2026, with letters, invites and
forwards ([ADR 0039](adr/0039-a-letter-is-one-call-and-a-day-with-no-time-is-a-reminder.md)) on
top of meal ideas, household jobs and calendar sync, model `deepseek-flash` through both
adapters: the 113 cases, 747 seconds. "Before" is `main` as this change was put on top of it, the
"after" of calendar sync [below](#previous-full-run-calendar-sync): the 103 cases it had.

| Adapter | Before | After | NOOP | Undo | Input tokens after (cached) | Output tokens after |
| --- | --- | --- | --- | --- | --- | --- |
| `openai_compat` | 103/103 | **113/113** | 3/3 | 7/7 | 1,891,543 (1,472,128) | 30,583 |
| `anthropic` | 102/103 | **112/113** | 3/3 | 7/7 | 1,909,471 (1,854,715) | 31,321 |

| Category | `openai_compat` before | after | `anthropic` before | after |
| --- | --- | --- | --- | --- |
| inventory | 12/12 | 12/12 | 12/12 | 11/12 |
| shopping list | 5/5 | 5/5 | 5/5 | 5/5 |
| NOOP | 3/3 | 3/3 | 3/3 | 3/3 |
| undo | 7/7 | 7/7 | 7/7 | 7/7 |
| calendar | 8/8 | 8/8 | 8/8 | 8/8 |
| reminders | 5/5 | 5/5 | 5/5 | 5/5 |
| photos | 6/6 | 6/6 | 6/6 | 6/6 |
| onboarding | 13/13 | 13/13 | 12/13 | 13/13 |
| presence | 4/4 | 4/4 | 4/4 | 4/4 |
| product | 16/16 | 17/17 | 16/16 | 17/17 |
| meals | 10/10 | 10/10 | 10/10 | 10/10 |
| household jobs | 14/14 | 14/14 | 14/14 | 14/14 |
| letters (new) | not run | 9/9 | not run | 9/9 |

The release bar (95% overall and 100% on NOOP and undo, on at least two providers) is met on both
endpoints. It is one sample per case, and the two endpoints are one model on two wire formats.

**One case that passed before failed after**, on `anthropic`: "batch finish with staple". The
rows were right and the reply was `AC`, where the case expects `ACK`. This is the case that fails
about one run in ten ([below](#previous-full-run-the-pass-on-repeat-mistakes)). It was run ten
more times on each endpoint on `main` and on this change: 18 of 20 on `main`, 20 of 20 on this
change.

**The same question was asked one rebase earlier**, when this change sat on meal ideas and
household jobs and the suite had 107 cases: 107/107 on `openai_compat` and 106/107 on
`anthropic`, 700 seconds. The one failure was "bought one thing off the list", the eggs logged
as `added` where the case expects `restocked`; the stock and the list ended right. It had passed
on `main`. Five more runs on each endpoint gave 10 of 10 on `main` and 6 of 10 on this change,
which looked like this change's doing; fifteen more on each gave 26 of 30 on both. Over the 40
runs a side that is 36 against 32, which does not tell them apart. It is the failure the 7 Oct
runs and the meal-ideas run had.

**What the tool costs.** A one-step turn reads 6,966 input tokens on `main` and 8,174 with this
change (the three NOOP cases, on both endpoints): 1,208 more on every model step, all in the
cached part of the prompt. About three quarters are the tool's description and its eleven
arguments, the rest the rule in the static prompt and the guide's lines. It is the dearest tool
in the list. Money: the account balance read $4.12 before the first run for this change and
$2.30 after the last, but three other changes were running evals on the same key all the while,
so that is not the cost of these runs and cannot be split. These runs together read about 19.5
million input tokens (about 90% cached) and wrote about 0.4 million. A 61-case run of 1.18
million input tokens is recorded below at 2 to 4 cents, which puts these at 35 to 65 cents: the
whole suite was run six times, once before and five times after, because the code changed twice
and `main` moved three times; a seventh was cut short at 46 runs by the second of those moves.

What the letters cases do not show: a real photo of a real letter, with a fold, a shadow or
handwriting; the model the household runs on, which was not run; and whether a reply was right
beyond the rows it left.

**Before it was put on top of the other three changes**, this change was built and run on the
71 cases `main` had that morning. One run of the whole suite before and after, on both adapters.
Before: the 71 cases, 457 seconds. After: the same 71, the 9 letters cases and one more product
case, 553 seconds.

| Adapter | Before | After | NOOP | Undo | Input tokens after (cached) | Output tokens after |
| --- | --- | --- | --- | --- | --- | --- |
| `openai_compat` | 69/71 | **81/81** | 3/3 | 7/7 | 1,098,094 (1,031,552) | 23,342 |
| `anthropic` | 71/71 | **81/81** | 3/3 | 7/7 | 1,097,676 (1,056,882) | 23,980 |

| Category | `openai_compat` before | after | `anthropic` before | after |
| --- | --- | --- | --- | --- |
| inventory | 12/12 | 12/12 | 12/12 | 12/12 |
| shopping list | 4/5 | 5/5 | 5/5 | 5/5 |
| NOOP | 3/3 | 3/3 | 3/3 | 3/3 |
| undo | 7/7 | 7/7 | 7/7 | 7/7 |
| calendar | 7/7 | 7/7 | 7/7 | 7/7 |
| reminders | 4/4 | 4/4 | 4/4 | 4/4 |
| photos | 6/6 | 6/6 | 6/6 | 6/6 |
| onboarding | 12/13 | 13/13 | 13/13 | 13/13 |
| presence | 4/4 | 4/4 | 4/4 | 4/4 |
| product | 10/10 | 11/11 | 10/10 | 11/11 |
| letters (new) | not run | 9/9 | not run | 9/9 |

Nothing that passed before failed after. The two failures before were on `openai_compat`:
"bought one thing off the list" logged the eggs as `added` where the case expects `restocked`
(the same answer as in the run below), and "quiet hours for the other adult by name" set Ada's
quiet hours to start at 20:00 where she had said 7pm. Both passed after; nothing in this change
touches either, so that is the model answering differently. The release bar (95% overall and 100%
on NOOP and undo, on at least two providers) is met on both endpoints in the run after.

**That table is the third whole run with the change, not the first.** The code changed after each
of the first two, so each is reported:

| Whole run | `openai_compat` | `anthropic` | What failed, and what was done |
| --- | --- | --- | --- |
| First | 79/81 | 80/81 | Below |
| Second | 81/81 | 81/81 | Nothing failed. Three small changes followed (a clock time such as "Wed 3.10 pm" is not read as a date; a doubt that rests on a wrong weekday sum is overruled; one call takes up to 50 entries, not 20), so it was run again |
| Third, the code that was then rebased | 81/81 | 81/81 | Nothing failed |

The three failures of the first run:

| Case | Endpoint | What the rows showed | What was done |
| --- | --- | --- | --- |
| a weekday that does not match its date is asked about, not guessed | `openai_compat` | The parents' evening was added for Thursday 12 November. The model had left the weekday out of that one entry, so the check had nothing to compare | The weekday is now read from the turn's own words in code, and believed over the model's ([agent-and-tools.md](agent-and-tools.md#letters-invites-and-forwards)) |
| the same case | `anthropic` | Nothing was added. The model called no tool and answered as if it had: "here's what went on the calendar", with the reminder times | The tool's description now says nothing is on the calendar until the call has answered. Not seen again in 48 further runs of the two cases that send this file, 24 on each endpoint; one sentence is not proof it is gone |
| batch finish with staple | `openai_compat` | Rows right; the reply was a sentence where the case expects `ACK`. This case fails about one run in ten ([below](#previous-full-run-the-pass-on-repeat-mistakes)) | Nothing. It passed 10 of 10 when sampled next, and in both later whole runs |

**The letters cases, five samples each**, at the code of the second whole run: 45/45 on
`openai_compat` and 44/45 on `anthropic`, 473 seconds, 1,411,493 input tokens (1,287,808 cached).
The one failure was "pasted text leaves out the date that has passed": the model held the
Christmas play back, saying "10 December 2026 is a Monday" of the Thursday the text had right. A
doubt of that kind is now overruled in code. Before that, the two weekday cases, "batch finish
with staple" and two product cases were run five times each on both endpoints: 50/50.

## Previous full run (calendar sync)

One run of the whole suite with the loop runtime, 10 Oct 2026, before and after two-way calendar
sync ([ADR 0038](adr/0038-a-connected-calendar-is-a-second-copy-kept-in-step.md)), model
`deepseek-flash` through both adapters. Before is `main` with meal ideas and household jobs in
it: 97 cases, 602 seconds. After is the same 97 and the 6 new ones: 634 seconds. The change adds
no tool. It adds about 80 tokens to the guide where Google Calendar is not switched on and about
190 where it is (by length), and one clause to the brief's line about the person speaking.

| Adapter | Before | After | NOOP | Undo | Input tokens after (cached) | Output tokens after |
| --- | --- | --- | --- | --- | --- | --- |
| `openai_compat` | 97/97 | **103/103** | 3/3 | 7/7 | 1,464,455 (1,126,144) | 24,694 |
| `anthropic` | 95/97 | **102/103** | 3/3 | 7/7 | 1,462,367 (1,421,568) | 23,773 |

| Category | `openai_compat` before | after | `anthropic` before | after |
| --- | --- | --- | --- | --- |
| inventory | 12/12 | 12/12 | 12/12 | 12/12 |
| shopping list | 5/5 | 5/5 | 5/5 | 5/5 |
| NOOP | 3/3 | 3/3 | 3/3 | 3/3 |
| undo | 7/7 | 7/7 | 7/7 | 7/7 |
| calendar | 7/7 | 8/8 | 7/7 | 8/8 |
| reminders | 5/5 | 5/5 | 5/5 | 5/5 |
| photos | 6/6 | 6/6 | 6/6 | 6/6 |
| onboarding | 13/13 | 13/13 | 13/13 | 12/13 |
| presence | 4/4 | 4/4 | 4/4 | 4/4 |
| product | 11/11 | 16/16 | 10/11 | 16/16 |
| meals | 10/10 | 10/10 | 10/10 | 10/10 |
| household jobs | 14/14 | 14/14 | 13/14 | 14/14 |

The release bar (95% overall and 100% on NOOP and undo, on at least two providers) is met on both
endpoints in both runs. It is one sample per case, and the two endpoints are one model on two
wire formats.

The three failures, all on the `anthropic` endpoint:

| Case | Run | What happened |
| --- | --- | --- |
| the MOT is due on a day | before | The job was made for everyone where the case expects it to be the speaker's |
| whose job is whose is on the Jobs page | before | The reply named the Jobs page and what it does, without the word the case checks for |
| quiet hours for the other adult by name | after | Ada's quiet hours began at 20:00 where "after 7pm" is 19:00 |

The third passed before and failed after, so it was run ten more times on each endpoint on
`main` and on this change: 18 of 20 before and 18 of 20 after, every failure the same 20:00.
The same was done on an earlier base (below): 19 of 20 each way. It is a slip the model makes
about one time in fifteen with or without this change, and the meal-ideas run lost it too.

**An earlier pair of runs, before meal ideas and household jobs were merged**, is how two of the
new cases came to read as they do. On the 71 cases of that morning `main` scored 71/71 on both
endpoints, and this change with its six cases 77/77 on `openai_compat` and 74/77 on `anthropic`.
One of the three was the quiet-hours case above (then 21:00). The other two were new cases that
asked for more than they were about. The calendar case wanted the dentist's only participant to
be Ada, and the model had added the speaker too; what the case asks is whether there is still
one event, and there was. "Is my Google calendar connected?" was answered "Yes — your Google
Calendar is connected", and the case wanted the calendar's name in the reply. Both were cut
back to what the case is about, and the six new cases were then run five times each on both
endpoints: 60 of 60. A run in between, on `main` with meal ideas only, gave 80/81 and 81/81
before and 87/87 on both endpoints after.

**What these cases cannot show** is the sync itself. No model takes part in it: it is code,
tested on rows.

## Previous full run (household jobs)

One run of the whole suite with the loop runtime, 10 Oct 2026, with household jobs
([ADR 0037](adr/0037-a-household-job-is-a-reminder-that-is-to-be-done.md)) on top of meal ideas,
model `deepseek-flash` through both adapters: 97 cases, 566 seconds. "Before" is a run of the 71
cases the same day on the code before both changes (414 seconds); the 81 cases with meal ideas
alone are the run [below](#previous-full-run-meal-ideas).

| Adapter | Before | After | NOOP | Undo | Input tokens after (cached) | Output tokens after |
| --- | --- | --- | --- | --- | --- | --- |
| `openai_compat` | 71/71 | **97/97** | 3/3 | 7/7 | 1,396,816 (1,086,976) | 22,643 |
| `anthropic` | 71/71 | **97/97** | 3/3 | 7/7 | 1,385,151 (1,343,740) | 24,319 |

| Category | `openai_compat` before | after | `anthropic` before | after |
| --- | --- | --- | --- | --- |
| inventory | 12/12 | 12/12 | 12/12 | 12/12 |
| shopping list | 5/5 | 5/5 | 5/5 | 5/5 |
| NOOP | 3/3 | 3/3 | 3/3 | 3/3 |
| undo | 7/7 | 7/7 | 7/7 | 7/7 |
| calendar | 7/7 | 7/7 | 7/7 | 7/7 |
| reminders | 4/4 | 5/5 | 4/4 | 5/5 |
| photos | 6/6 | 6/6 | 6/6 | 6/6 |
| onboarding | 13/13 | 13/13 | 13/13 | 13/13 |
| presence | 4/4 | 4/4 | 4/4 | 4/4 |
| product | 10/10 | 11/11 | 10/10 | 11/11 |
| meals | not run | 10/10 | not run | 10/10 |
| household jobs (new) | not run | 14/14 | not run | 14/14 |

Nothing that passed before failed after. The release bar (95% overall and 100% on NOOP and undo,
on at least two providers) is met on both endpoints. It is one sample per case, and the two
endpoints are one model on two wire formats.

**What the cases found while the change was being built.** Each of these was a run of the job
and reminder cases, three samples a case on both endpoints unless it says otherwise.

- **A reminder that was asked for became a job.** With `add_job` on offer, "remind us every
  Sunday at 6pm to put the bins out" went to it in 4 runs of 6: the person would have been asked
  to answer "done" every week and asked again on Mondays. One-off reminders were not affected
  (18 of 18). Both tool descriptions now say that anything someone asks to be reminded of is
  `set_reminder`, a chore included, and the next 6 runs were 6 of 6. The reminder cases no longer
  count a job as a reminder, so this cannot come back unseen.
- **A birthday without "every year".** "Grandma's birthday is on 12 November" was set up without
  a repeat in 3 runs of 6. A date now repeats yearly by rule, in code.
- **"Later" with a time of the model's own.** Asked "later" with no time, the model worked the
  two hours out itself in 7 runs of 10, and once made it 21:00, two hours after the nudge, where
  code makes it 21:10, two hours after the answer. The description now says to leave the time
  out for a bare "later" (it was left out in both runs of the final suite), and the case
  accepts both times: the person is asked again that evening either way.
- **Whose prescription.** "Ada's prescription needs picking up this Friday" was given to the
  speaker in 1 run of 2. The owner's description now says that something named as one of the
  family's is theirs unless they say who does it; 6 of 6 after.

With the first of those changes the job, reminder and Jobs-page cases were 117 of 120 over three
samples, the three being the birthday, fixed afterwards. An earlier whole-suite run of this change, before it was
put on top of meal ideas, is not in the table: a mistake in the harness (a loop variable named
`key`, which hid the function that normalises item names) failed every case that names an item,
56 runs. Of the 118 runs it could not touch, 117 passed, the one being "later" at 21:00.

**What it costs.** The two tools' definitions, the longer `set_reminder` description and the
guide's lines on jobs are about 5,900 characters, so roughly 1,500 more tokens on every model
step, in the part of the prompt that is cached. A household with jobs also has one more line in
its brief.

**What the job cases do not show.** A tick on a nudge is code and is covered by unit tests, not
here. Nothing here runs the worker: that a nudge goes out at its time, and what it says, are
unit tests on a controlled clock.

## Previous full run (meal ideas)

One run of the whole suite with the loop runtime, 10 Oct 2026, before and after meal ideas
([ADR 0036](adr/0036-meal-ideas-from-a-short-stock-read.md)), model `deepseek-flash` through both
adapters. Before: the 71 cases, 424 seconds. After: the same 71 and the 10 meal cases, 455 seconds.

| Adapter | Before | After | NOOP | Undo | Input tokens after (cached) | Output tokens after |
| --- | --- | --- | --- | --- | --- | --- |
| `openai_compat` | 71/71 | **81/81** | 3/3 | 7/7 | 897,671 (739,328) | 18,857 |
| `anthropic` | 70/71 | **79/81** | 3/3 | 7/7 | 892,115 (854,912) | 19,646 |

| Category | `openai_compat` before | after | `anthropic` before | after |
| --- | --- | --- | --- | --- |
| inventory | 12/12 | 12/12 | 12/12 | 12/12 |
| shopping list | 5/5 | 5/5 | 5/5 | 4/5 |
| NOOP | 3/3 | 3/3 | 3/3 | 3/3 |
| undo | 7/7 | 7/7 | 7/7 | 7/7 |
| calendar | 7/7 | 7/7 | 7/7 | 7/7 |
| reminders | 4/4 | 4/4 | 4/4 | 4/4 |
| photos | 6/6 | 6/6 | 6/6 | 6/6 |
| onboarding | 13/13 | 13/13 | 12/13 | 13/13 |
| presence | 4/4 | 4/4 | 4/4 | 4/4 |
| product | 10/10 | 10/10 | 10/10 | 10/10 |
| meals (new) | not run | 10/10 | not run | 9/10 |

The release bar (95% overall and 100% on NOOP and undo, on at least two providers) is met on both
endpoints in both runs. It is one sample per case, and the two endpoints are one model on two
wire formats.

The three failures, all on the `anthropic` endpoint:

| Case | Run | What happened |
| --- | --- | --- |
| quiet hours for the other adult by name | before | Ada ended at 20:00 to 08:00 where the case expects 19:00 to 08:00 |
| bought one thing off the list | after | The eggs were logged as `added` where the case expects `restocked`; the stock and the list ended right. It passed in the before run, so it was run five more times on each side: 4/5 on the code before this change and 5/5 after it. It is the failure the 7 Oct runs had, on the same endpoint |
| a child's allergy keeps the peanut butter out of the kids' tea | after | The answer was right: "2. Eggy bread with carrot sticks (no peanuts)". The check failed any meal line with "peanut" in it. It now lets a line pass that rules the thing out |

**The meal cases, three more samples each** on both endpoints with that check corrected: 59/60,
and `meal_ideas` was called in every run that asked what to cook. The one failure was the case
that is not a question. It then read "I'm doing jollof tonight, be home by 7 if you want it
hot", and one run of six booked "Jollof rice dinner" for 19:00 with a reminder. `meal_ideas` was
not called in it: the time in the message was taken for a plan. The eight earlier runs of that
message had ignored it. It was then run 24 more times after this change and 36 times on the code
before it, and was ignored in all 60: one booking in 38 runs after against none in 36 before does
not say the change caused it, and does not rule it out. The case now reads
"I'm doing jollof tonight, hope everyone's hungry", which was ignored in 24 runs of 24: it is
there to show that the new tool does not answer a statement, not to test times in group chat.

**What it costs.** One model step reads 177 more tokens than before (4,898 to 5,075, the
provider's own count for the prompt and the tools with and without the change), all in the
cached part. The same 71 cases cannot be compared across the two runs by their totals, because
the after total includes the meal cases.

**What the meal cases do not show**: whether a suggested meal can be made from what was said to
be there. Of the replies read, one called a bolognese complete with no tomatoes in the house.
They also do not show a pick made from a suggestion the model wrote in the same run: the two
pick cases start from a suggestion written for the case. That flow was run by hand on both
endpoints (ask, pick, ask how to make it, undo) and did what it should.

## Previous full run (the product guide)

One run of the whole suite with the loop runtime, 7 Oct 2026, before and after the product guide
([ADR 0034](adr/0034-the-assistant-is-told-what-is-built.md)), model `deepseek-flash` through
both adapters. Before: the 61 cases, 366 seconds. After: the same 61 and the 10 product cases, 433
seconds.

| Adapter | Before | After | NOOP | Undo | Input tokens after (cached) | Output tokens after |
| --- | --- | --- | --- | --- | --- | --- |
| `openai_compat` | 61/61 | **71/71** | 3/3 | 7/7 | 790,196 (623,232) | 17,249 |
| `anthropic` | 60/61 | **70/71** | 3/3 | 7/7 | 774,443 (739,049) | 17,484 |

| Category | `openai_compat` before | after | `anthropic` before | after |
| --- | --- | --- | --- | --- |
| inventory | 12/12 | 12/12 | 12/12 | 12/12 |
| shopping list | 5/5 | 5/5 | 4/5 | 4/5 |
| NOOP | 3/3 | 3/3 | 3/3 | 3/3 |
| undo | 7/7 | 7/7 | 7/7 | 7/7 |
| calendar | 7/7 | 7/7 | 7/7 | 7/7 |
| reminders | 4/4 | 4/4 | 4/4 | 4/4 |
| photos | 6/6 | 6/6 | 6/6 | 6/6 |
| onboarding | 13/13 | 13/13 | 13/13 | 13/13 |
| presence | 4/4 | 4/4 | 4/4 | 4/4 |
| product (new) | not run | 10/10 | not run | 10/10 |

Nothing that passed before failed after. The one failure is the same case in both runs, on the
`anthropic` endpoint only: "bought one thing off the list". After "I got the eggs" the model
logged the eggs as `added` where the case expects `restocked`; the stock and the list ended
right both times. The release bar (95% overall and 100% on NOOP and undo, on at least two
providers) is met on both endpoints in both runs. It is one sample per case, and the two
endpoints are one model on two wire formats.

**What the guide costs.** The same 61 cases took 579,853 input tokens before and about 740,500
after on `openai_compat` (the after-run's total less the product cases), which is 2,330 more per
turn over their 69 turns. A turn is two or three model steps, and one step reads 990 more tokens
than before (4,987 against 3,995, measured on the three one-step NOOP cases): about 945 for the
guide, which is in the cached part of the prompt, and about 45 for the brief's three lines. The
account balance, shown to the cent, read $4.32 before the first run and $4.14 after the last, so
every run for this change together cost 18 cents: the whole suite twice, and the product cases
about fifteen times over. The first whole-suite run moved it by 2.

**The product cases, three samples each**, with the guide left out, in the prompt (as built),
and behind a tool. The tool was a `how_to` call that returned the same guide; it was written for
this comparison and is not in the code.

| | `openai_compat` | `anthropic` | Seconds for the 60 runs | Input tokens for the 60 runs |
| --- | --- | --- | --- | --- |
| No guide (one sample, 20 runs) | 5/10 | 5/10 | | |
| Guide in the prompt | 30/30 | 30/30 | 116 | 298,398 |
| Guide behind a `how_to` tool | 30/30 | 29/30 | 171 | 559,838 |

Without the guide the assistant answered "switch me to WhatsApp" with "Switched — I'll message
you on WhatsApp from now on", said yes to reading photos in a household where it cannot, took
"the shop thing" for the shopping list, told one person the list is at "the same link as the ICS
feed your calendar uses" and another to look for a calendar called "Home" in their calendar app,
and said the list was on "the dashboard" without saying how to get there. With the tool the model
called it in all 60 runs; its one failure was an answer about the shopping list to "how does the
shop thing work?". Sixty runs do not tell 60 from 59 apart: the choice rests on the extra model
step and on rules that have to be read on turns that are not how-to questions (ADR 0034).

One more run of the product cases, twice each, through `claude_code` with `claude-sonnet-5-5`,
the provider and model this household runs on: **20/20**, 52 seconds, 153,311 input tokens
(129,914 cached). Nothing was billed for it.

What the product cases do not show: whether a reply was right beyond the word they check. Of the
60 replies in the three-sample run above, one said a chat app is added "on the Chat apps page",
which no page does; the guide says so, and five of the six answers to that question got it right.

## Previous full run (the pass on repeat mistakes)

One run of the whole suite with the loop runtime, 6 Oct 2026, after the pass on repeat mistakes
([ADR 0031](adr/0031-rules-the-model-kept-breaking.md)), 61 cases, model `deepseek-flash`
through both adapters, 432 seconds.

| Adapter | Endpoint | Passed | NOOP | Undo | Input tokens (cached) | Output tokens |
| --- | --- | --- | --- | --- | --- | --- |
| `openai_compat` | `https://api.deepseek.com/` | **61/61** | 3/3 | 7/7 | 589,398 (514,684) | 15,317 |
| `anthropic` | `https://api.deepseek.com/anthropic` | **61/61** | 3/3 | 7/7 | 589,744 (560,633) | 15,759 |

| Category | `openai_compat` | `anthropic` |
| --- | --- | --- |
| inventory | 12/12 | 12/12 |
| shopping list | 5/5 | 5/5 |
| NOOP | 3/3 | 3/3 |
| undo | 7/7 | 7/7 |
| calendar | 7/7 | 7/7 |
| reminders | 4/4 | 4/4 |
| photos | 6/6 | 6/6 |
| onboarding | 13/13 | 13/13 |
| presence | 4/4 | 4/4 |

The release bar (95% overall and 100% on NOOP and undo, on at least two providers) was **met on
both endpoints in this run**, for the first time. The 50 cases of the earlier runs are all in
the 61, so they stand at 50/50 on both. Two things limit what that says. It is one sample per
case, and the two endpoints are one model on two wire formats, not two providers' models.

Cost: the account balance, shown to the cent, read $4.37 before the run and $4.33 after it.

**The cases behind the pass, five samples each.** The 24 cases the pass was about (the ones
that kept failing, the related ones, and the new variants) were run five times on each endpoint
before any change and again at the final code, with tool calls printed:

| | `openai_compat` | `anthropic` |
| --- | --- | --- |
| Before (23 cases, 115 runs each) | 81/115 | 94/115 |
| After (24 cases, 120 runs each) | 120/120 | 119/120 |

| Case | Before | After |
| --- | --- | --- |
| batch finish with staple | 0/10 | 9/10 |
| two things run out in the group and only the staple is listed (new) | 7/10 | 10/10 |
| long receipt ignores totals and savings | 2/10 | 10/10 |
| receipt restocks what was bought | 7/10 | 10/10 |
| receipt lines land on the household's own items (new) | 8/10 | 10/10 |
| quiet hours changed later just by talking | 5/10 | 10/10 |
| quiet hours for the speaker alone, said another way (new) | 0/10 | 10/10 |
| quiet hours for the other adult by name (new) | 5/10 | 10/10 |
| gone corrected to getting low (new) | 7/10 | 10/10 |
| taking back a list add, said another way (new) | 9/10 | 10/10 |
| taking back a run-out, said another way (new) | 6/10 | 10/10 |
| bought with a stated quantity | 9/10 | 10/10 |
| the other 11 that were run before | 110/110 | 110/110 |
| a finished non-staple is listed when the person asks (new, written after the first run) | not run | 10/10 |

The one failure after: "batch finish with staple" answered "Eggs and bread logged as finished —
eggs are on the shopping list." where the case expects `ACK`. The rows were right and the
sentence is true; before the pass the usual sentence said bread was on the list as well. So that
case still fails about one run in ten, and a run of the whole suite can read 60/61.

"Taking back a run-out" was reworded between the two runs. Its second message first read "oops,
ignore that, there's another bag", and in all 4 of its failures the model recorded the one bag
it had just been told about (in 2 of them after undoing the mistake), which is a fair reading.
It now reads
"oops, ignore that, I was wrong". Its before and after are therefore not the same question.

## The subscription providers

One run of the whole suite through each subscription provider
([llm.md](llm.md#subscription-providers)), 7 Oct 2026, 61 cases, loop runtime, on a laptop with
Claude Code 2.1.292 and Codex CLI 0.154.0 signed in to their subscriptions. The models are each
CLI's small one, not `deepseek-flash`, so this compares providers as they would be used and not
one model across four adapters.

| Provider | Model | Passed | NOOP | Undo | Input tokens (cached) | Output tokens | Whole run | Per turn |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `claude_code` | `haiku` (Claude Haiku 4.5) | **58/61** (95.1%) | 3/3 | 7/7 | 769,724 (591,234) | 44,156 | 606 s | 8.7 s mean, 7.2 s median |
| `codex_cli` | `gpt-5.6-luna` | **59/61** (96.7%) | 3/3 | 7/7 | 811,093 (442,624) | 10,985 | 825 s | 11.9 s mean, 10.3 s median |
| `openai_compat`, `anthropic` (above) | `deepseek-flash` | 61/61 | 3/3 | 7/7 | 589,398 and 589,744 | 15,317 and 15,759 | 432 s for both | about 3.1 s mean |

| Category | `claude_code` | `codex_cli` | `openai_compat`, `anthropic` |
| --- | --- | --- | --- |
| inventory | 11/12 | 11/12 | 12/12 |
| shopping list | 5/5 | 5/5 | 5/5 |
| NOOP | 3/3 | 3/3 | 3/3 |
| undo | 7/7 | 7/7 | 7/7 |
| calendar | 7/7 | 7/7 | 7/7 |
| reminders | 4/4 | 3/4 | 4/4 |
| photos | 5/6 | 6/6 | 6/6 |
| onboarding | 12/13 | 13/13 | 13/13 |
| presence | 4/4 | 4/4 | 4/4 |

Both are over the release bar's numbers (95% overall, 100% on NOOP and undo) in this one run,
`claude_code` by one case. It is one sample per case on each.

Time per turn is the case's wall time over its turns (69 turns in the 61 cases), seeding
included. A turn is two or three model steps, and each step starts the CLI. `codex_cli`'s mean
includes one step that reached the 120-second limit.

The five that failed, and what three more runs of each showed (tool calls printed):

| Provider | Case | In the full run | Three more runs |
| --- | --- | --- | --- |
| `claude_code` | batch finish with staple | Rows right; replied "Eggs on the list. Add bread too?" where the case expects `ACK` | 2/3. The same case fails about one run in ten on `deepseek-flash` |
| `claude_code` | routines step books a weekly activity | Booked the first Chatterbox for Tuesday 13 October, not 6 October | 3/3, after a change to the adapter (below) |
| `claude_code` | receipt restocks what was bought | Milk restocked twice, once with source `shopping` | 3/3 |
| `codex_cli` | a finished item that is not a staple is listed when the person asks | No event recorded | 3/3 |
| `codex_cli` | a reminder set for late tonight is sent despite quiet hours | The CLI gave no answer within 120 seconds, so the turn failed | 3/3, each in under 16 seconds. Why that one step hung was not found |

**The date.** Both CLIs tell the model today's date on their own, from the machine's clock. The
suite pins the clock to Monday 5 October; the runs were on Wednesday 7 October, and from a
Wednesday the next Tuesday is the 13th. That fits the Chatterbox failure but was not proved to be
its cause. Outside the suite the two dates differ only around midnight, when the machine's time
zone is not the household's. The adapters now tell the model that the brief's date is the one to
go by. The three re-runs above were with that change; the full runs were without it.

**What it used.** Nothing was billed. On the Claude subscription the five-hour window read 4%
used before the day's work and 15% after all of it (development calls, both small runs, the full
run and the re-runs, and the session that did the work), and the weekly window went from 3% to
4%. Codex's allowance was not read.

## Previous full run (milestone 6)

One run of the whole suite with the loop runtime, 6 Oct 2026 (milestone 6), 50 cases, model
`deepseek-flash` through both adapters, 299 seconds. No case and no prompt changed since the
previous run; the Anthropic adapter's fix from that run is in.

| Adapter | Endpoint | Passed | NOOP | Undo | Input tokens (cached) | Output tokens |
| --- | --- | --- | --- | --- | --- | --- |
| `openai_compat` | `https://api.deepseek.com/` | **48/50** (96%) | 3/3 | **3/4** | 426,902 (405,502) | 12,776 |
| `anthropic` | `https://api.deepseek.com/anthropic` | **48/50** (96%) | 3/3 | 4/4 | 440,912 (418,560) | 14,845 |

| Category | `openai_compat` | `anthropic` |
| --- | --- | --- |
| inventory | 7/8 | 7/8 |
| shopping list | 5/5 | 5/5 |
| NOOP | 3/3 | 3/3 |
| undo | 3/4 | 4/4 |
| calendar | 7/7 | 7/7 |
| reminders | 4/4 | 4/4 |
| photos | 5/5 | 4/5 |
| onboarding | 10/10 | 10/10 |
| presence | 4/4 | 4/4 |

The release bar (95% overall and 100% on NOOP and undo, on at least two providers) is met on the
Anthropic-compatible endpoint in this run and **not on the OpenAI-compatible one**, where one
undo case failed. So it is not met. In the previous run it was the other way round. One sample
per case: these are the same model on two wire formats, and a case that passes on one and fails
on the other mostly shows how much one run varies.

Cost: the account balance, shown to the cent, read $4.78 before the run and $4.77 after it.

Failures, from the assertion messages of this run:

| Case | Endpoint | What the rows showed |
| --- | --- | --- |
| batch finish with staple | both | Bread was on the list as well as eggs: the model added it itself. This case has failed in every run |
| correction undoes then records what was meant | `openai_compat` | After "no, I meant we're low on milk" the milk row was `low` with quantity 0 where the case expects the 2 that were there before the mistaken "finished" |
| long receipt ignores totals and savings | `anthropic` | An event with a source other than `receipt`: after logging the receipt the model also ticked an item off the list, a second restock of the same purchase |

## The Letta comparison

Spec section 8.4: the Letta runtime is "promoted only if it beats the loop on the eval suite".
**It did not, so the loop stays the default and Letta stays off.**

| Runtime | Passed | NOOP | Undo | Model calls | Input tokens (cached) | Output tokens | Seconds |
| --- | --- | --- | --- | --- | --- | --- | --- |
| loop, `openai_compat` ([milestone 6](#previous-full-run-milestone-6)) | 48/50 | 3/3 | 3/4 | not counted | 426,902 (405,502) | 12,776 | 299 for both |
| loop, `anthropic` (milestone 6) | 48/50 | 3/3 | 4/4 | not counted | 440,912 (418,560) | 14,845 | |
| **Letta** | **45/50** (90%) | 3/3 | 3/4 | 118 | 506,802 (310,400) | 13,679 | 246 |

| Category | Letta |
| --- | --- |
| inventory | 7/8 |
| shopping list | 5/5 |
| NOOP | 3/3 |
| undo | 3/4 |
| calendar | 7/7 |
| reminders | 4/4 |
| photos | 5/5 |
| onboarding | 9/10 |
| presence | 2/4 |

One run, 6 Oct 2026, the same 50 cases and the same model. Cost: $4.70 before, $4.69 after.
It predates the pass on repeat mistakes and was not repeated after it, so it is compared with the
loop's run of that day, not with the latest one.

| Case that failed under Letta | What the rows showed |
| --- | --- |
| yes to the low-stock prompt puts the guessed items on the list | Nothing was added. Letta answered "yes" with no idea what had been asked |
| yes to the out-and-about offer reads the list back | The reply was "Yes to what? I don't have anything pending" |
| batch finish with staple | Bread on the list as well as eggs, as with the loop |
| undo a list change | Bleach was still on the list after "undo" |
| quiet hours changed later just by talking | Both adults ended at 20:00, where only the speaker should have |

**The two presence failures are the design, not the model.** The low-stock prompt and the "out
and about" offer are written by code and sent through the outbox. The loop sees them because it
reads the thread's history from Postgres. Letta keeps its own history, which only holds what
went through Letta, so it never saw the question it was being answered. The same would be true
of every reminder, brief and arrival list. A Letta runtime that was going to be used would have
to be told about what the assistant said on its own.

**How it was run**, because none of it is the default:

- **Server:** `letta/letta:0.16.8` in Docker, the last tag of that image that serves the REST
  API `letta-client` wraps. It started without an embedding model or any other setup.
- **Model:** DeepSeek through Letta's OpenAI-compatible provider (`OPENAI_BASE_URL`), handle
  `openai-proxy/deepseek-flash`. Letta's own DeepSeek provider only knows the model names
  `deepseek-chat` and `deepseek-reasoner`, which DeepSeek no longer lists.
- **A pass-through in front of DeepSeek** (`tests/evals/letta_deepseek_shim.py`). Without it no
  turn with a tool call completes: Letta shortens tool-call ids to 29 characters, and DeepSeek's
  thinking mode refuses a conversation whose ids it did not issue ("The `reasoning_content` in
  the thinking mode must be passed back to the API"). The pass-through restores the ids and
  hands the reasoning back, and changes nothing else. The loop needs none of this.
- **Two runs.** The first scored 32/50 and measured this integration, not Letta: every tool
  argument that was optional failed inside Letta ("Unsupported type: None"), because Letta
  0.16.8 cannot coerce a value declared as `anyOf` a type or null, which took out every
  reminder and most of setup; and the first version of the pass-through did not hand reasoning
  back, which DeepSeek demands when the turn has a photo. Both were fixed (the first in
  `app/agent/letta_runtime.py`) and the suite was run again. The table is the second run.

```sh
DEEPSEEK_OPENAI_BASE_URL=https://api.deepseek.com uv run python tests/evals/letta_deepseek_shim.py &
docker run -d --name letta -p 127.0.0.1:8283:8283 --add-host host.docker.internal:host-gateway \
  -e OPENAI_API_KEY="$EVAL_API_KEY" -e OPENAI_BASE_URL=http://host.docker.internal:9911/v1 letta/letta:0.16.8
RUN_EVALS=1 EVAL_RUNTIME=letta EVAL_LETTA_MODEL=openai-proxy/deepseek-flash uv run pytest tests/evals
```

What the comparison does not show: Letta with a model it supports natively, Letta's current
harness (a different product with a different API), or anything about long conversations, which
is where Letta's own memory is meant to help and which no case in this suite exercises.

## Previous full run (milestone 5)

One run of the whole suite, 6 Oct 2026 (milestone 5), 50 cases, model `deepseek-flash` through
both adapters. Five cases are new: four answers to messages the assistant
starts, and setup ending with the presence links.

| Adapter | Endpoint | Passed | NOOP | Undo | Input tokens (cached) | Output tokens |
| --- | --- | --- | --- | --- | --- | --- |
| `openai_compat` | `https://api.deepseek.com/` | **48/50** (96%) | 3/3 | 4/4 | 437,831 (388,736) | 11,329 |
| `anthropic` | `https://api.deepseek.com/anthropic` | **44/50** (88%) | 3/3 | **3/4** | 424,775 (397,696) | 13,894 |

| Category | `openai_compat` | `anthropic` |
| --- | --- | --- |
| inventory | 7/8 | 7/8 |
| shopping list | 5/5 | 5/5 |
| NOOP | 3/3 | 3/3 |
| undo | 4/4 | 3/4 |
| calendar | 7/7 | 7/7 |
| reminders | 4/4 | 4/4 |
| photos | 4/5 | 4/5 |
| onboarding | 10/10 | 9/10 |
| presence (new) | 4/4 | 2/4 |

The release bar (95% overall and 100% on NOOP and undo, on at least two providers) is met on the
OpenAI-compatible endpoint in this run and **not met on the Anthropic-compatible one**, so it is
not met. One sample per case: the 48 could be a 46 next time.

Cost: the account balance, shown to the cent, read $4.82 before the run and $4.81 after it.

Failures, from the assertion messages of this run:

| Case | Endpoint | What the rows showed |
| --- | --- | --- |
| batch finish with staple | `openai_compat` | Rows correct; the reply was "Eggs and bread are on the shopping list." where the case expects `ACK`, and bread is not on it |
| batch finish with staple | `anthropic` | Bread was on the list as well as eggs: the model added it itself. This case has failed in every run |
| long receipt ignores totals and savings | both | An event with a source other than `receipt`: after logging the receipt the model also ticked an item off the list, a second restock of the same purchase |
| undo a list change | `anthropic` | After "add bleach" then "undo", bleach was still on the list. The same was seen on the other endpoint in milestone 1, where the trace showed `undo_last` running and the model then adding bleach again |
| quiet hours changed later just by talking | `anthropic` | Ada said "don't message me after 8pm" and both adults ended at 20:00 to 07:00. Third run in a row |
| yes to the low-stock prompt puts the guessed items on the list | `anthropic` | Milk and bread were still only guesses |
| yes to the out-and-about offer reads the list back | `anthropic` | The reply was `NOOP` |

**The last two were a defect in the Anthropic adapter, found by these cases.** It dropped a
thread's opening message when the assistant had sent it, because that API wants a user turn
first (`app/llm/anthropic.py`), so "yes" reached the model with nothing to answer. The adapter
now keeps the message behind a placeholder user turn. The four presence cases were then run once
more on that endpoint only and all four passed. That re-run is not part of the score above, and
the rest of the suite was not run again: no other case starts with an assistant message, so the
change does not reach them.

## Previous full run (milestone 4)

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
