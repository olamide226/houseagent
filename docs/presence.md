# Presence and predictions

Two things happen without anyone asking: the list for a shop arrives when you walk into it, and
the assistant asks about things that are probably running low. Both are optional. If nobody sets
up a phone, everything else works as before.

## Arriving at a shop

Your iPhone can tell the assistant when you arrive at a shop, and the assistant sends you the
shopping list for that shop. There is no app to install. The assistant never learns where you
are: it only ever hears "Ola arrived at Tesco Extra".

### Getting your link

Send the word **shops** to the assistant in your chat. It answers with your personal link. Open
the link on your iPhone: the page it opens shows the steps below with a button for each one.

At the end of setup the assistant tells each adult about this once, in a short message, and sends
nothing more until someone asks. An adult can also be given a link from the dashboard: Settings,
"The list when you reach a shop", "Make link".

The link is yours alone. Whoever has it can tell the assistant that you arrived somewhere, so do
not pass it on or post a screenshot of it. If you lose it, "Replace link" on Settings makes a new
one and the old one stops working.

### Setting it up, one shop at a time

Do these three steps for one shop, then again for the next.

1. **Copy the link for the shop.** On your page, tap **Copy** beside the shop's name.
2. **Add the shortcut.** Tap **Get the shortcut**, then **Get Shortcut**. When it asks for the
   link, paste what you copied. Before you add a second shop, rename the shortcut you already
   have to its shop's name: open it, tap the arrow beside its name, then **Rename**.
3. **Tell your iPhone when to use it.** Open the shortcut in the Shortcuts app and tap **Edit**,
   then **Automation**. Choose **Arrive**, tap **Choose** and find the shop. Then tap **Edit**
   again, open **Privacy** and turn on **Allow Running When Locked**, so it runs by itself.

On an iPhone that has not been updated for a while, step 3 is different: in the Shortcuts app tap
**Automation**, then **+**, and choose **Arrive**. Tap **Choose**, find the shop and tap **Next**.
Pick the shortcut you added. Then tap the new automation, turn off **Ask Before Running** and tap
**Don't Ask**.

To try it, put something on the shopping list and run the shortcut once. The list for that shop
should arrive in your chat.

If your page says "Nearly ready" where the steps should be, whoever set up the assistant for your
family has one thing to do first. It is the next section.

**Leaving home.** With two more shortcuts the assistant can offer you the list as you head out
with a lot on it. Under "Also when you leave home" your page has two more links. Do the same three
steps with each, choosing your home as the place: **Arrive** for the first and **Leave** for the
second.

## For whoever set the assistant up

This part is technical and is done once for the whole family, on an iPhone.

### What Apple allows

Read from Apple's Shortcuts User Guide on 7 October 2026 (its iOS 27, iOS 26 and iOS 18 editions,
fetched as pages, not summaries). **None of it has been tried on a phone.**

| Apple's guide says | So |
| --- | --- |
| A shortcut can be shared as an iCloud link; whoever taps it and then **Get Shortcut** has it in their collection ("Share shortcuts") | One person builds the shortcut, everyone else adds it |
| A field with an *import question* is cleared when the shortcut is shared, and the person adding it is asked for their own value ("Add import questions to shared shortcuts") | The shared shortcut holds nobody's link |
| "Automation shortcuts are specific to a device" ("Intro to shortcuts with automations"). No page describes sharing or installing an automation | The Arrive step cannot be sent to anyone. Each person makes it on their own phone, once per shop. There is no one-tap setup |
| No page describes an automation passing anything to the shortcut it runs | A shortcut cannot be told which shop it is for when it runs, so there is one shortcut per shop, each holding that shop's link |
| iOS 27: a trigger is added to a shortcut with **Edit**, **Automation**; it runs unasked with **Privacy**, **Allow Running When Locked**. iOS 26 and 18: **Automation**, **+**, **Create Personal Automation**, a trigger, **Next**, then "use an existing shortcut"; it runs unasked with **Ask Before Running** off and **Don't Ask** ("Add automations", "Create a new personal automation", "Enable or disable a personal automation") | The two versions of step 3 |
| **Get Contents of URL** has **Show More**, where the method can be POST ("Request your first API") | The one action the shortcut needs |

Not in Apple's guide, so not known:

- Which fields an import question can be attached to. The steps below assume the URL of **Get
  Contents of URL** is one of them. If it is not offered, put the link in a **Text** action above
  it, use that text as the URL, and attach the question to the Text action.
- Whether the question is asked when the shortcut is added or the first time it runs. The guide
  says "when the recipient runs the shortcut".
- What happens when the same shared shortcut is added a second time. The steps tell people to
  rename the first copy before adding another, in case the second would replace it.
- Whether a shortcut shared from iOS 27 carries its automation with it. The steps assume not.
- "Run Immediately", which is what iOS 17 and 18 show on the trigger's own screen according to
  the spec this app was built from, is on none of Apple's pages.

### Sharing the shortcut, once

1. Send **shops** to the assistant and open your link on your iPhone. Tap **Copy** beside any
   shop.
2. In the Shortcuts app make a new shortcut. Add the action **Get Contents of URL** and paste the
   link where it says URL. Tap **Show More** and change **Method** to **POST**. Leave the rest.
3. Name it, for example "Tell Home I'm at the shop", and run it once. With something on that
   shop's list, the list arrives in your chat.
4. Open the shortcut's details, tap **Setup**, then **Add New Question**. Choose the link you
   pasted and type the question: `Paste the link you copied for this shop`.
5. Share the shortcut with **Copy iCloud Link**.
6. Set `PRESENCE_SHORTCUT_URL` to that link ([operations.md](operations.md#configuration)) and
   restart the api.

From then on every adult's page has a **Get the shortcut** button in place of these steps, and
nobody else sees the words POST or URL. Until it is set, the page of the household's admin shows
steps 2 to 5, and everyone else's says to ask them.

### The links

A personal link is `https://home.example.com/presence/<43 characters>`. Three things answer on it:

| Request | What it does |
| --- | --- |
| `GET /presence/{token}`, and `GET` of any link below | The page: what the link is for, and the steps. Reads only; nothing is recorded, so a chat app's link preview or a tap in the browser is harmless. An unknown token gets a page saying the link has stopped working (404) |
| `POST /presence/{token}/{event}/{place}` | A ping with nothing to fill in: `event` is `enter` or `exit`, `place` is the place's name, URL-encoded. This is what a link copied from the page is |
| `POST /presence/{token}` with `{"event": "enter" \| "exit", "place": "<name>"}` | The same ping, as the spec has it |

The place name must be one the assistant knows: the shops said during setup, listed under Places
on Settings. Case and extra spaces do not matter. The page writes the names into the links, so
nobody types one.

By hand, without the shared shortcut, an automation's one action is **Get Contents of URL** with a
link copied from the page and **Method** set to **POST**. From a terminal:

```sh
curl -i -X POST https://home.example.com/presence/<token>/enter/Tesco%20Extra
curl -i -X POST https://home.example.com/presence/<token> \
     -H 'Content-Type: application/json' -d '{"event": "enter", "place": "Tesco Extra"}'
```

A POST is always answered `204 No Content`, whether or not the token or the body was any good, so
the response cannot be used to find out whether a link exists. To see what happened, look at the
worker and api logs (`presence`, `presence_ignored`, `presence_rate_limited`).

### What a ping does

A valid ping, in either form, is stored in `presence_events` and one rule runs
(`app/presence/rules.py`, no model involved):

| Rule | When | Condition | What happens |
| --- | --- | --- | --- |
| Store arrival | `enter` a place of kind `store` | Something is on the list for this shop, not counting guesses; no list was sent to this person for this shop in the last 2 hours | A DM with the list for this shop, then any "(probably)" entries. Sent at once, even in quiet hours |
| Out and about | `exit` a place of kind `home` | The list has 8 or more entries, or one was added in the last 2 hours; once per person per day | A DM: "You're out. The list has 9 items, want it?" Answering "yes" gets the list |
| Both home | `enter` a place of kind `home` | No adult's phone last said they left home, and a shop list was sent today | Nothing is sent. Today's "out and about" is marked used for every adult |

The list "for this shop" is every entry that names no shop, plus those whose shop reads like this
one: "Tesco" on an entry matches the place "Tesco Extra", "African shop" matches "African shop on
Rye Lane", and "Costco" matches neither.

A place name the assistant has not heard before is added with kind `other` (or `home`, if it is
called Home) and does nothing until someone gives it a kind on the Settings page. A token is good
for 30 pings an hour.

Decisions and their reasons: [ADR 0023](adr/0023-presence-endpoint-and-tokens.md),
[ADR 0024](adr/0024-presence-rules-and-nudge-dedupe.md),
[ADR 0033](adr/0033-the-presence-link-opens-a-page-and-is-sent-on-request.md).

## Predictions

### The consumption model

For each item the assistant learns how long it usually lasts, from that item's own history in
`inventory_events` (`app/services/consumption.py`):

- A **cycle** runs from the latest `restocked` or `added` to the next `finished`, `low` or
  `discarded`. Cycles shorter than 12 hours or longer than 120 days are ignored.
- The **average** is an exponentially weighted mean of cycle lengths, the newest cycle counting
  for half: cycles of 6, 4 and 6 days give 5.5 days.
- With **two or more** cycles, and the item bought since it last ran out, the predicted run-out is
  the last purchase plus the average. Otherwise there is no prediction.

Undone events are not learned from. The result is one row per item in `consumption_profiles`.

### "Probably" entries on the list

An item predicted to run out within two days goes on the shopping list as a guess (`reason =
'predicted'`). Guesses are shown apart from what was asked for: "(probably)" in chat, "Probably
needed" on the Shopping page, a trailing section in the list sent at a shop.

| What happens | The guess |
| --- | --- |
| Someone asks for the item, or says it is low or finished | Becomes a real entry |
| The item is bought | Is ticked off with it |
| Someone takes it off the list | Stays off until the item has been bought again |
| A week passes after the predicted day with no word | Is dropped: the guess was wrong |
| "Got everything on the list" | Is left alone; only what was asked for is marked bought |

### The jobs

| Job | When, household time | What it does |
| --- | --- | --- |
| `consumption_model` | Once a day, from 03:00. If the worker was down then, as soon as it is back | Recomputes every profile and the guesses. Then asks the model once per household to put items with no category into a supermarket section (`LLM_FAST_MODEL`), so lists are grouped by aisle |
| `low_stock_prompt` | 17:30, or up to four hours late | Recomputes, then sends the household "Probably running low: milk, bread. Add to the list?" for guesses nobody was asked about in the last three days. Nothing to ask, nothing sent |
| `weekly_digest` | Sunday 18:00 | Now also names what will probably run low in the week ahead |

A "yes", "just the milk", or a tick reaction to the prompt is an ordinary message: the agent sees
the question in the thread and updates the list. Reasons: [ADR 0026](adr/0026-consumption-model-and-guessed-list-entries.md).

## Limits

- The phone steps have not been tried on a phone, and Apple renames these screens between iOS
  versions. What is and is not in Apple's guide is listed under
  [What Apple allows](#what-apple-allows).
- iOS decides when an Arrive or Leave automation fires. It can be a minute late, fire twice, or
  not fire with Location Services or Background App Refresh off. A double ping sends one list.
- The model learns from what it is told. If nobody says the milk ran out, it has no cycle to
  learn from. Receipts that log "semi skimmed milk" beside "milk" split one item's history in two;
  merge the duplicates on the item's page.
- A prediction is per item, not per pack size: a bigger pack looks like a slower household.
