# 0042 A shop link is replaced by asking, follows the app's address, and needs nobody to share a shortcut

## Context

The family trying the assistant reported in October 2026 that their shop links did not work. On
the server, no ping had ever been recorded. The only two links ever sent in chat (7 and 9 Oct)
held the address of an early test run, a temporary `trycloudflare.com` name that stopped existing
when the app moved to its own domain. A shortcut built from either calls nothing. And
`PRESENCE_SHORTCUT_URL` had never been set, so every page but the admin's said "Nearly ready, ask
whoever set this up" ([ADR 0033](0033-the-presence-link-opens-a-page-and-is-sent-on-request.md)).

Three things were missing:

- **No way back from a dead link.** The word `shops` only made a first link. Someone who had one
  was sent to Settings, a page they reach through the word `dashboard`. A token is stored only as
  its hash ([ADR 0023](0023-presence-endpoint-and-tokens.md)), so the same link cannot be sent
  again.
- **Nothing noticed a change of address.** A link is `PUBLIC_BASE_URL` plus the token. When that
  address changes, every link and every shortcut made from one stops working, and nobody is told.
- **Everyone waited for the admin.** ADR 0033 asked the admin to build one shortcut with an
  import question and share it. Until then, nobody else could finish.

What Apple allows, read from Apple's Shortcuts User Guide on 26 October 2026 (iOS 27 and iOS 26
editions, and the Mac edition for files and signing):

- **A shortcut can be shared as an iCloud link or as a file** ("Share shortcuts"). A file is
  shared for *Anyone*, and then "Apple will receive a copy of your shortcut for validation (to
  prevent unauthorized tampering when you share it)", or for *People Who Know Me*, and then "your
  contact info will be included in the shortcut file". On a Mac, `shortcuts sign` does the same
  from the command line with the same two modes, and Apple receives a copy for validation
  ("Run shortcuts from the command line").
- **The Shortcuts URL scheme can open the app, create an empty shortcut
  (`shortcuts://create-shortcut`), open or run one by name, and search the Gallery** ("Open and
  create a shortcut using a URL scheme", "Run a shortcut using a URL scheme"). It has no way to
  add a shortcut from a link or a file.
- **Automations are specific to a device.** On iOS 27 they are backed up to iCloud and sync to
  other devices turned off ("Intro to shortcuts with automations"); on iOS 26 they "will not
  sync to other devices" ("Intro to personal automation"). No page describes sharing one.
- **Get Contents of URL** offers GET, POST, PUT, PATCH and DELETE under Show More ("Request your
  first API"). The guide does not say which one a new action starts with.

So a file for each person cannot be made by the server. Each file would hold that person's token,
and an importable one has to be signed. Signing needs a Mac and an Apple Account, and Apple keeps
a copy of every file signed for *Anyone*. Apple documents neither the file's format nor what
happens to an unsigned one. One tap to set it all up is not possible. What can go is the admin's
step.

## Decision

- **`shops` from someone who has a link asks first.** "Would you like a new one? Your old link,
  and any shortcut you made with it, would stop working. Reply yes for a new one." The first
  message they then send in their own chat answers it: a yes (a few words such as yes, ok, sure,
  or a tick or thumbs-up) within an hour replaces the link and sends the new one with one line
  saying the old one stopped working. Anything else goes to the model as usual, and a later yes
  replaces nothing. A yes in the family group is never the answer. Code does this, not the
  model, because the reply holds a secret (`members.presence_renew_offered_at`,
  `app/pipeline/inbound.py`). The question is there because a new token silently breaks every
  shortcut made with the old one, and the word `shops` is also what people send to find out what
  it is.
- **A link made for an old address is replaced at once.** Asking would protect nothing: that
  link cannot work. The message says that the web address changed.
- **Each link records the address it was made for** (`members.presence_link_base`, migration
  `0005`). The worker's `presence_links` job runs at start and hourly. When the address the app
  runs with now is not that one, it sends each connected adult one plain message saying the
  link has stopped working and that the word `shops` gets a new one. The message waits for quiet
  hours. Its outbox dedupe key is the link's hash and the new address, so restarts and replicas
  send it once per link and change, and a second change of address is told again.
- **Links from before this change.** The migration reads the address back from the message
  that sent the link, matching the token's hash, and keeps nothing else from it. On the server
  that gives the two links their temporary address, so both adults are told and `shops` replaces
  at once. A link only ever shown on Settings has no such message and stays unknown. An unknown
  address is treated as possibly old. The holder is told once that the link "may no longer
  work", and `shops` asks before replacing it, because it may still be working.
- **The page and Settings say so.** A page opened from a link made for another address says the
  link was made for the old address and that the links on it use the new one. Settings says
  "has a link made for an old web address: it no longer works", or "perhaps made for an old web
  address" when that is not known. The brief tells the model the same for the person speaking.
- **A shop's link asked for with GET is the ping.** `GET /presence/{token}/{event}/{place}`
  records the arrival, as the POST does, and answers with a short "That worked" page. A HEAD
  records nothing. A shortcut made by hand is then one action, Get Contents of URL with the
  pasted link, and nobody changes its method. The personal link, `GET /presence/{token}`, is
  still a page that records nothing, so a chat app's preview of the link sent in chat does
  nothing. This amends ADR 0033, under which any GET recorded nothing.
- **Nobody waits for the admin.** Every page shows the same three steps: copy the shop's link,
  tap "Start a new shortcut" (`shortcuts://create-shortcut`), add Get Contents of URL and paste
  the link, name it after the shop, then add the Arrive automation. `PRESENCE_SHORTCUT_URL` is
  optional now. Where it is set, the second step is "Get the shortcut" as before. The admin's
  page has a short section saying that none of this has been tried on an iPhone.

## Consequences

- A household's adults can each get a working link without anyone else doing anything, from
  the one word they were already told about.
- Each person builds a one-action shortcut per shop: a few more taps than adding a shared one,
  and no import question, whose behaviour Apple's guide leaves open (which fields take one, when
  it is asked).
- A shop's link pasted into a chat app is fetched for its preview, and that is an arrival: the
  list is sent, once in two hours. Links copied from the page are meant for the Shortcuts app,
  and the page says to keep them private.
- An unknown-address link gets one "may no longer work" message even if it still works. The only
  links that can be unknown were made on Settings before this change.
- If `PUBLIC_BASE_URL` is set wrongly by mistake, every adult with a link is told their link
  stopped working. Setting it back sends nothing more: a link made for the right address is
  not told.
- None of the iPhone steps has been tried on an iPhone. The two versions of step 3 come from the
  iOS 27 and iOS 26 guides.
- Older links stay readable in old messages and in the outbox. This change does not remove them.
