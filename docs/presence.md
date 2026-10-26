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
not pass it on or post a screenshot of it.

### If your link stopped working, or you lost it

Send **shops** again. The assistant asks whether you would like a new link, because a new one
stops the old one, and every shortcut you made with it, from working. Reply **yes** (or ok, or
put a thumbs-up on the question) and the new link arrives; its page walks you through setting it
up again. Any other answer leaves your link as it is, and so does a yes more than an hour later.

If your link was made for an old web address of the assistant, it cannot work any more, so
**shops** sends the new one straight away, without asking.

On Settings, "Replace link" does the same for any adult.

### Setting it up, one shop at a time

Do these three steps for one shop, then again for the next. Nobody has to do anything first.

1. **Copy the link for the shop.** On your page, tap **Copy** beside the shop's name.
2. **Make a shortcut for the shop.** Tap **Start a new shortcut**: the Shortcuts app opens on a
   new, empty shortcut. Tap **Add Action**, search for **Get Contents of URL** and tap it. Tap
   **URL** in it and paste what you copied. Leave the rest as it is. Then tap **New Shortcut** at
   the top, tap **Rename**, and call it after the shop.
3. **Tell your iPhone when to use it.** Open the shortcut in the Shortcuts app and tap **Edit**,
   then **Automation**. Choose **Arrive**, tap **Choose** and find the shop. Then tap **Edit**
   again, open **Privacy** and turn on **Allow Running When Locked**, so it runs by itself.

On an iPhone that has not been updated for a while, step 3 is different: in the Shortcuts app tap
**Automation**, then **+**, and choose **Arrive**. Tap **Choose**, find the shop and tap **Next**.
Pick the shortcut you made. Then tap the new automation, turn off **Ask Before Running** and tap
**Don't Ask**.

To try it, put something on the shopping list and run the shortcut once: tap it in the Shortcuts
app. The list for that shop should arrive in your chat.

**Leaving home.** With two more shortcuts the assistant can offer you the list as you head out
with a lot on it. Under "Also when you leave home" your page has two more links. Do the same three
steps with each, choosing your home as the place: **Arrive** for the first and **Leave** for the
second.

## For whoever set the assistant up

There is nothing you have to do for the family. Each adult sets up their own phone from their own
page. Your page has one more section, "For you, as whoever set up Home", saying what has not been
tried.

### What Apple allows

Read from Apple's Shortcuts User Guide on 26 October 2026: its iOS 27 and iOS 26 editions, and
its Mac edition for files and signing. The pages were read whole, not as summaries. **None of it
has been tried on a phone.**

| Apple's guide says | So |
| --- | --- |
| A shortcut can be shared as an iCloud link; whoever taps it and then **Get Shortcut** has it in their collection ("[Share shortcuts](https://support.apple.com/guide/shortcuts/share-shortcuts-apdf01f8c054/ios)") | One person could build a shortcut and everyone else add it. That is optional now ([below](#sharing-one-shortcut-optional)) |
| A shortcut can also be shared as a file, for *Anyone* ("Apple will receive a copy of your shortcut for validation") or for *People Who Know Me* ("your contact info will be included in the shortcut file"). On a Mac, `shortcuts sign` signs an exported file in the same two modes, and "Apple receives a copy for validation" ("[Run shortcuts from the command line](https://support.apple.com/guide/shortcuts-mac/run-shortcuts-from-the-command-line-apd455c82f02/mac)") | The server cannot make each person a file with their link in it: an importable file is signed, signing takes a Mac and an Apple Account, and every file signed for *Anyone* is sent to Apple. The guide does not describe the file's format, or what happens to an unsigned one |
| The Shortcuts URL scheme opens the app, creates an empty shortcut (`shortcuts://create-shortcut`), opens or runs a shortcut by name, and searches the Gallery ("[Open and create a shortcut using a URL scheme](https://support.apple.com/guide/shortcuts/open-create-and-run-a-shortcut-apda283236d7/ios)", "[Run a shortcut using a URL scheme](https://support.apple.com/guide/shortcuts/run-a-shortcut-from-a-url-apd624386f42/ios)") | There is no link that adds a shortcut with its action filled in. The page's **Start a new shortcut** button is `shortcuts://create-shortcut` |
| iOS 27: "Automation shortcuts are specific to a device. The automation will be backed up to iCloud but will sync to other devices with the automation disabled" ("[Intro to shortcuts with automations](https://support.apple.com/guide/shortcuts/intro-to-automation-shortcuts-apd690170742/ios)"). iOS 26: personal automation "will not sync to other devices" | The Arrive step cannot be sent to anyone. Each person makes it on their own phone, once per shop. There is no one-tap setup |
| No page describes an automation passing anything to the shortcut it runs | A shortcut cannot be told which shop it is for when it runs, so there is one shortcut per shop, each holding that shop's link |
| iOS 27: "Tap New Shortcut, tap Rename"; "Tap Add Action" ("[Create a custom shortcut](https://support.apple.com/guide/shortcuts/create-a-custom-shortcut-apd84c576f8c/ios)"). A trigger is added to a shortcut with **Edit**, **Automation**; it runs unasked with **Privacy**, **Allow Running When Locked** ("[Add automations](https://support.apple.com/guide/shortcuts/add-automations-apdfbdbd7123/ios)"). iOS 26: **Automation**, **+**, **Create Personal Automation**, a trigger, **Next**, then a blank automation or "use an existing shortcut"; it runs unasked with **Ask Before Running** off and **Don't Ask** ("Create a new personal automation", "Enable or disable a personal automation") | Steps 2 and 3, and the older step 3 |
| **Get Contents of URL** has **Show More**, where the method can be GET, POST, PUT, PATCH or DELETE ("[Request your first API](https://support.apple.com/guide/shortcuts/request-your-first-api-apd58d46713f/ios)") | The one action the shortcut needs. The guide does not say which method a new action starts with, so a shop's link is a ping by GET and by POST alike, and nobody changes the method |

Not in Apple's guide, so not known:

- What the Get Contents of URL action looks like on screen, and so whether "tap **URL** in it" is
  exactly what a person sees.
- Whether `shortcuts://create-shortcut` opens from a button on a web page in Safari, or asks
  first. Apple says the scheme can be used "anywhere a URL can be used", including a web browser.
- "Run Immediately", which is what iOS 17 and 18 show on the trigger's own screen according to
  the spec this app was built from, is on none of Apple's pages.

### Sharing one shortcut (optional)

A household can still share one shortcut, so each person adds it and pastes their link instead
of making one. It depends on import questions, which the guide leaves partly open: which fields
take one, and whether the question is asked when the shortcut is added or "when the recipient
runs the shortcut", as the guide says.

1. Make the shortcut as in step 2 above, from any shop's link on your page, and run it once.
2. Open the shortcut's details, tap **Setup**, then **Add New Question**. Choose the link you
   pasted and type the question: `Paste the link you copied for this shop`.
3. Share it with **Copy iCloud Link**.
4. Set `PRESENCE_SHORTCUT_URL` to that link ([operations.md](operations.md#configuration)) and
   restart the api.

From then on step 2 on everyone's page is **Get the shortcut**, then **Get Shortcut** and paste.

### When the address changes

A link is the app's address (`PUBLIC_BASE_URL`) followed by the person's token, and each link
records the address it was made for. When the worker starts, and every hour, it compares them.
Each connected adult whose link was made for another address is sent one message, held until
their quiet hours end: their shop link and any shortcut made with it have stopped working, and
the word **shops** gets a new one. Each link is told once per address, across restarts and
replicas; if the address changes again, they are told again.

Their page and Settings also say so: the page, when the old address still reaches the app, says
the link was made for the old address and that the links on it use the new one.

Links made before the address was recorded (migration `0005`) were given the address of the chat
message that sent them. A link only ever shown on Settings has no such message, so its address
is not known. It is treated as possibly old: its holder is told once that it "may no longer
work", and **shops** asks before replacing it, since it may still be working. Reasons:
[ADR 0042](adr/0042-a-shop-link-is-replaced-by-asking-and-follows-the-address.md).

### The links

A personal link is `https://home.example.com/presence/<43 characters>`. Three things answer on it:

| Request | What it does |
| --- | --- |
| `GET /presence/{token}` | The page: what the link is for, and the steps. Reads only; nothing is recorded, so a chat app's link preview or a tap in the browser is harmless. An unknown token gets a page saying the link has stopped working (404) |
| `POST` or `GET /presence/{token}/{event}/{place}` | A ping with nothing to fill in: `event` is `enter` or `exit`, `place` is the place's name, URL-encoded. This is what a link copied from the page is. A GET answers with a short "That worked" page; a HEAD records nothing |
| `POST /presence/{token}` with `{"event": "enter" \| "exit", "place": "<name>"}` | The same ping, as the spec has it |

The place name must be one the assistant knows: the shops said during setup, listed under Places
on Settings. Case and extra spaces do not matter. The page writes the names into the links, so
nobody types one.

From a terminal:

```sh
curl -i https://home.example.com/presence/<token>/enter/Tesco%20Extra
curl -i -X POST https://home.example.com/presence/<token>/enter/Tesco%20Extra
curl -i -X POST https://home.example.com/presence/<token> \
     -H 'Content-Type: application/json' -d '{"event": "enter", "place": "Tesco Extra"}'
```

A POST is always answered `204 No Content`, whether or not the token or the body was any good, so
the response cannot be used to find out whether a link exists. A GET of a shop's link, like the
page, does tell a working link from a dead one (ADR 0033). To see what happened, look at the
worker and api logs (`presence`, `presence_ignored`, `presence_rate_limited`).

A shop's link pasted into a chat app is fetched for its preview, and that counts as arriving: the
list is sent, at most once in two hours. The links on the page are for the Shortcuts app only.

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
[ADR 0033](adr/0033-the-presence-link-opens-a-page-and-is-sent-on-request.md),
[ADR 0042](adr/0042-a-shop-link-is-replaced-by-asking-and-follows-the-address.md).

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
