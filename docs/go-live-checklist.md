# Go-live checklist

What is still open before this is offered to the public. One household uses it today on one
server, `houseagent.devng.host`, since 9 Oct 2026; nothing below stops that. Last checked against
the code, the docs and that trial on 10 Oct 2026, with meal ideas, household jobs, Google
Calendar, letters and spending merged. Apple Calendar was added the same day and has its own
heading.

Each line has a status and a pointer to where the detail is.

- **Not done**: something has to be built, bought, decided or written.
- **Not verified**: it is built, and has never been tried against the real thing.
- **Done**: tick the box, change the status, and name the pull request or the date it was tried.
  Leave the line in place.

A pull request that opens or closes an item changes this page in the same change. Where a line
points to a "build record", the detail is in the notes kept while that part was built. Those are
not in this repository, so the line itself is all there is here.

The first three groups and the last are about the product as a whole. What is open about one of
the five features added on 10 Oct 2026 is under that feature's own heading, whatever kind of
item it is.

## Never tried against the real service

- [ ] **Not verified.** WhatsApp against Meta: the signature check, every payload, media, sends.
  There is no Meta account yet. [channels.md](channels.md#whatsapp)
- [ ] **Not verified.** WhatsApp members are identified by user id, on the strength of Meta's docs
  alone. [ADR 0019](adr/0019-whatsapp-members-are-identified-by-user-id.md)
- [ ] **Not done.** The WhatsApp reminder template is submitted and approved as Utility. Without
  it nothing reaches a WhatsApp-only member after 24 hours.
  [channels.md](channels.md#template-approval)
- [ ] **Not verified.** A WhatsApp group created by webhook. It needs an Official Business
  Account, and anyone holding the invite link can join.
  [ADR 0021](adr/0021-whatsapp-groups-are-created-asynchronously.md)
- [ ] **Not verified.** iMessage through a real BlueBubbles server, Mac and iPhone.
  [channels.md](channels.md#imessage)
- [ ] **Not done.** BlueBubbles posts each webhook once. A message sent while the api is away is
  lost, and nothing makes up for it. [channels.md](channels.md#known-limits-2)
- [ ] **Not verified.** The BlueBubbles outage runbook has never been followed.
  [operations.md](operations.md#bluebubbles-outage)
- [ ] **Not verified.** The iPhone Shortcut on a phone: the automation fires and the list arrives.
  [presence.md](presence.md#limits)
- [ ] **Not done.** The owner makes the shared Shortcut once and sets `PRESENCE_SHORTCUT_URL` on
  the server. [presence.md](presence.md#sharing-the-shortcut-once)
- [ ] **Not verified.** A receipt or fridge photo sent from a phone to the server.
  [operations.md](operations.md#media-storage)
- [x] **Done, 9 Oct 2026.** A voice note sent from a phone to the server. The owner sent one
  through Telegram; it was transcribed and answered. [llm.md](llm.md#speech-to-text)
- [ ] **Not done.** Photos kept in a private S3-compatible bucket with a lifecycle rule. ImgBB
  links can be opened by anyone who has them. [operations.md](operations.md#media-storage)
- [ ] **Not verified.** A voice note with three changes is acknowledged within 10 seconds (p95).
  Never measured. [spec.md](spec.md#16-testing-and-acceptance-criteria)
- [ ] **Not verified.** A calendar app subscribed to the calendar feed.
  [dashboard.md](dashboard.md#calendar-feed)
- [x] **Done, by 10 Oct 2026.** A login link in a real Telegram chat is no longer spent by the
  preview. The owner asked for one in Telegram on the server and it opened the dashboard.
  [ADR 0035](adr/0035-a-login-link-is-spent-by-its-button.md)
- [ ] **Not verified.** The Helm chart installed on a cluster. Linted and rendered only.
  [operations.md](operations.md#deploying-with-helm)
- [x] **Done, by 10 Oct 2026.** The Claude subscription in a signed-in container. The server
  answers on the owner's Claude subscription with `claude-sonnet-5-5`, on Docker Compose.
  [operations.md](operations.md#the-claude-subscription-on-a-server)
- [ ] **Not verified.** What happens on the server when that sign-in expires or the allowance
  runs out. Codex in a signed-in container, and either provider on a cluster, are untried too.
  [operations.md](operations.md#when-the-sign-in-expires-or-the-allowance-runs-out)
- [ ] **Not done.** Decide whether a public product may answer on a Claude or ChatGPT
  subscription, as the trial server does. The vendors' terms are unclear, and OpenAI's were not
  read on the page. [llm.md](llm.md#what-the-vendors-terms-say)

## Quality and operations

- [ ] **Not done.** Evals run nightly in CI. Today they are run by hand.
  [evals.md](evals.md#running)
- [ ] **Not done.** Evals pass on a second model with an API key, several samples a case. The bar
  is met by one model on two wire formats. [evals.md](evals.md#latest-results)
- [ ] **Not verified.** Meal ideas, household jobs, Google and Apple Calendar, letters and spending
  on the model the server answers with. Their eval cases were run on `deepseek-flash` only;
  `claude-sonnet-5-5` has run the 10 product cases alone, twice each, 20 of 20.
  [evals.md](evals.md#previous-full-run-the-product-guide)
- [ ] **Not done.** Choose the subscription providers' models. The whole suite was run on the
  small ones, which scored 58 and 59 of 61 in one run, and never on the one the server uses.
  [evals.md](evals.md#the-subscription-providers)
- [ ] **Not done.** The prompt has grown with each new tool and nobody has set a limit. One model
  step read 3,995 input tokens before the product guide and reads 9,714 now, before any of the
  conversation: about 990 for the guide, 177 for meal ideas, 1,500 for household jobs, 80 to 190
  for Google Calendar, 1,208 for letters and 1,540 for spending. Apple Calendar added 104 words of
  guide where it is switched on, and nothing where it is not. A turn is two or three steps, so
  19,000 to 29,000 tokens at the least, all in the cached part of the prompt. With an API key
  that is money on every turn of every household (eval runs on `deepseek-flash` cost 2 to 4
  cents for 1.18 million input tokens). On the subscription the server uses nothing is billed;
  it counts against the owner's allowance, and how much of it a day uses has not been measured.
  [evals.md](evals.md#latest-results)
- [ ] **Not done.** The server's database is backed up to another machine on a schedule. Today
  there are only the dumps taken by hand before each migration.
  [operations.md](operations.md#backup-and-restore)
- [ ] **Not done.** A restore from such a backup is practised. One restore was done, when the
  household moved on 9 Oct 2026. [operations.md](operations.md#not-covered)
- [ ] **Not done.** `CALENDAR_SECRET_KEY` is kept in the same backup as the database. Restored
  without it, no connected calendar can be used until its owner connects again. It opens each
  adult's stored Apple password as well as their Google sign-in, so it is guarded as a password
  is. [operations.md](operations.md#the-key)
- [x] **Done, by 10 Oct 2026.** Migrations `0002` (household jobs), `0003` (calendar sync) and
  `0004` (receipts) applied on the server, each after a dump taken by hand. Nothing went wrong.
  [operations.md](operations.md#update-and-roll-back)
- [ ] **Not done.** Going back to older code loses what the newer migration holds: undoing `0004`
  drops every price kept, `0003` every calendar connection and what came in from one, `0002`
  every household job. Only the first is written down, and none has been tried on the server.
  [operations.md](operations.md#update-and-roll-back)
- [ ] **Not done.** CI builds the image and publishes it.
  [operations.md](operations.md#deploying-with-helm)
- [ ] **Not verified.** The server comes back by itself after a reboot.
  [operations.md](operations.md#on-one-server)
- [ ] **Not done.** Login, shop and calendar tokens, and the code in the address Google sends a
  person back to, are kept out of the shared proxy's access log. [operations.md](operations.md#logs)
- [ ] **Not done.** Rate limits on webhooks and the dashboard. Only invites, login links and shop
  pings are limited, and the invite count resets on restart.
  [channels.md](channels.md#known-limits)
- [ ] **Not done.** What the public address exposes is reviewed again for the public deployment,
  and `/readyz` answering 503 gets a test. [operations.md](operations.md#on-one-server)
- [ ] **Not done.** Secrets pasted in chat during the trial are replaced: the Telegram bot token,
  Groq, ImgBB, DeepSeek, the Claude setup token. [operations.md](operations.md#token-rotation)
- [ ] **Not done.** A plan for scanners and abuse of the public address. Nothing is written yet,
  here or in a build record.

## Gaps a family will hit

- [ ] **Not done.** The assistant cannot send the login link itself. The person sends the word
  `dashboard`. [agent-and-tools.md](agent-and-tools.md#the-product-guide)
- [ ] **Not done.** Nobody can switch their chat app from chat, and an invite does not say where
  to find the assistant in WhatsApp or iMessage. [channels.md](channels.md#known-limits-1)
- [ ] **Not done.** A family member cannot be removed or renamed.
  [dashboard.md](dashboard.md#inviting-the-family)
- [ ] **Not done.** A shop or place cannot be renamed or removed. A copied shop link carries the
  name. [dashboard.md](dashboard.md#presence-links-and-places)
- [ ] **Not done.** Error pages are blank, and a refused form on Calendar, Family or Settings
  loses what was typed. Build record: dashboard redesign.
- [ ] **Not done.** The dashboard cannot be installed to a home screen: no web app manifest or
  touch icon. Build record: dashboard redesign.
- [ ] **Not done.** Dates and times follow one UK style, on the dashboard and in what the
  assistant sends ("Tue 6 Oct at 19:00"), not the household's locale. Build record: dashboard
  redesign.
- [ ] **Not done.** The list at the shop is iPhone only. There is nothing for Android.
  [presence.md](presence.md#what-apple-allows)
- [ ] **Not done.** Telegram replies show stray stars and backticks, because text is sent escaped.
  [channels.md](channels.md#payload-notes)
- [ ] **Not done.** Telegram audio files and video notes are not read, only voice messages.
  [channels.md](channels.md#payload-notes)
- [ ] **Not done.** WhatsApp audio in AMR or AAC is not converted, so it is not transcribed.
  Build record: voice notes and the presence link.
- [ ] **Not done.** A receipt can still add an item beside its twin ("coconut milk", "semi skimmed
  milk"). [ADR 0031](adr/0031-rules-the-model-kept-breaking.md)

## Meal ideas

- [ ] **Not verified.** Meal ideas asked from a phone in a real chat: the answer reads well in
  Telegram, and "the first one" puts what is missing on the list.
  [agent-and-tools.md](agent-and-tools.md#meal-ideas)
- [ ] **Not done.** Nothing checks that a suggested meal can be made from what it says is there,
  or that what it says is missing is not in the house. It is the model's word.
  [ADR 0036](adr/0036-meal-ideas-from-a-short-stock-read.md)
- [ ] **Not done.** An allergy or a diet is kept to because the model reads it, not because code
  knows what is in a dish. A public release says so to the household in plain words and does
  not present a suggestion as safe for someone with an allergy.
  [ADR 0036](adr/0036-meal-ideas-from-a-short-stock-read.md)
- [ ] **Not done.** Food past its date is named and left out, but the assistant does not know a
  use-by date from a best-before one. [ADR 0036](adr/0036-meal-ideas-from-a-short-stock-read.md)
- [ ] **Not done.** Something that is not food counts as food until the nightly sorting has run,
  so bleach bought today can be offered as something to cook with tonight.
  [ADR 0036](adr/0036-meal-ideas-from-a-short-stock-read.md)

## Household jobs and renewals

- [ ] **Not verified.** A household job in a real Telegram chat: the nudge arriving, "done" and
  "later", and a thumbs-up on the nudge ticking it off with the assistant's own thumbs-up put
  back on its message. Tried against recorded Telegram payloads only. The webhook has to list
  `message_reaction`, and in a group the assistant has to be an admin.
  [channels.md](channels.md#setup-checklist)
- [ ] **Not verified.** A tick on a job's nudge from WhatsApp or iMessage, and a job's nudge sent
  inside the WhatsApp reminder template after 24 hours.
  [ADR 0037](adr/0037-a-household-job-is-a-reminder-that-is-to-be-done.md)
- [x] **Done, 10 Oct 2026, with household jobs.** A reminder can be put off. "Later" in answer to
  a job's nudge asks again in two hours or the next morning, in code. "Later" in answer to a
  plain reminder has the assistant set it again two hours on, which is the model's doing and
  passed 6 runs of 6. [agent-and-tools.md](agent-and-tools.md#household-jobs-appservicesjobspy)
- [ ] **Not done.** A household job can only be added or changed in chat. The Jobs page ticks
  off, skips and stops. [dashboard.md](dashboard.md#pages)
- [ ] **Not done.** Household jobs and birthdays are not in the calendar feed, the morning brief
  or the weekly digest. [ADR 0037](adr/0037-a-household-job-is-a-reminder-that-is-to-be-done.md)
- [ ] **Not done.** A job cannot be counted from when it was last done ("every three months
  after the last time"). Every repeat is a fixed schedule.
  [ADR 0037](adr/0037-a-household-job-is-a-reminder-that-is-to-be-done.md)
- [ ] **Not done.** A family that never answers "done" is asked again the morning after every
  time. Nothing learns to stop asking, and nobody has lived with it yet. Decide after real use
  whether the morning-after message should be a setting.
  [agent-and-tools.md](agent-and-tools.md#household-jobs-appservicesjobspy)
- [ ] **Not done.** A job due on a day is always said at 09:00. A household cannot choose the
  time. [ADR 0037](adr/0037-a-household-job-is-a-reminder-that-is-to-be-done.md)

## Google Calendar

- [ ] **Not verified.** Google Calendar sync against Google: the sign-in, the first read, a
  change each way, a deleted and a moved date of a series, an all-day event. There are no Google
  credentials yet, and the ADR lists what was taken from Google's docs on trust. [ADR 0038](adr/0038-a-connected-calendar-is-a-second-copy-kept-in-step.md)
- [ ] **Not done.** The owner makes the Google OAuth client and sets `GOOGLE_CLIENT_ID`,
  `GOOGLE_CLIENT_SECRET` and `CALENDAR_SECRET_KEY` on the server.
  [operations.md](operations.md#google-calendar)
- [ ] **Not done.** The Google OAuth app is published and passes Google's review for its two
  calendar scopes. While it is in Testing only listed people can connect, and each connection
  stops after 7 days. [operations.md](operations.md#testing-or-in-production)
- [ ] **Not done.** An all-day event cannot be made in chat or on the Calendar page. One that
  comes from a connected calendar is kept, shown and named in the morning brief.
  [data-model.md](data-model.md#calendar-rows)
- [x] **Done, 10 Oct 2026, with Apple Calendar.** Apple Calendar can be connected, both ways. It
  is built and has not met iCloud: see [Apple Calendar](#apple-calendar) below. [ADR 0041](adr/0041-apple-calendar-is-caldav-with-an-app-specific-password.md)
- [ ] **Not done.** An event made in Google Calendar or on an iPhone can be reminded twice: by
  the calendar and by the assistant. [ADR 0038](adr/0038-a-connected-calendar-is-a-second-copy-kept-in-step.md)
- [ ] **Not done.** A change made in a connected calendar takes up to two minutes to arrive, and
  a date put back into a series in Google Calendar is not noticed (in Apple Calendar it is).
  [ADR 0038](adr/0038-a-connected-calendar-is-a-second-copy-kept-in-step.md)
- [ ] **Not done.** Calendar sync for many households: Google's push notifications in place of a
  read every two minutes, and a way to find changed events without comparing every one every 15
  seconds. [ADR 0038](adr/0038-a-connected-calendar-is-a-second-copy-kept-in-step.md)
- [ ] **Not done.** The privacy policy says what is read from a connected calendar: every event
  on the one calendar picked, shown to the whole household and, as part of what is coming up,
  sent to the model provider. [ADR 0038](adr/0038-a-connected-calendar-is-a-second-copy-kept-in-step.md)

## Apple Calendar

- [ ] **Not verified.** Apple Calendar sync against iCloud. It has only met a CalDAV server on
  the test machine; no Apple account existed. With a real account, check: the sign-in with an
  app-specific password and the list of calendars offered; the first read; a change each way,
  and that it is not sent back; a moved and a deleted date of a series; an all-day event; that
  iCloud accepts an event made here (its time zone, its name) and that the phone adds no alert
  of its own to it; and that a removed password stops the connection with one message. [ADR 0041](adr/0041-apple-calendar-is-caldav-with-an-app-specific-password.md)
- [ ] **Not verified.** What iCloud answers when it has forgotten our place in a calendar's
  changes, whether it returns an `ETag` when an event is written, and how often it lets itself
  be asked. Each was written to cope with more than one answer, and none was seen. [ADR 0041](adr/0041-apple-calendar-is-caldav-with-an-app-specific-password.md)
- [ ] **Not verified.** The two steps on the page for making an app-specific password. They are
  the words of Apple's own support page; nobody has followed them from this page.
  [dashboard.md](dashboard.md#a-connected-apple-calendar)
- [ ] **Not done.** `CALENDAR_SECRET_KEY` is set on the server. That alone lets an adult connect
  an Apple Calendar; nothing is set up at Apple. The server has to be able to call out to
  `caldav.icloud.com` and `pNN-caldav.icloud.com`. [operations.md](operations.md#apple-calendar)
- [ ] **Not done.** Connecting asks the person to make an app-specific password at Apple, which
  needs two-factor authentication on their account, and to make another whenever they change
  their Apple password. Nothing simpler is published that this app could use. [ADR 0041](adr/0041-apple-calendar-is-caldav-with-an-app-specific-password.md)
- [ ] **Not done.** The stored password is good for more than the one calendar picked. The
  privacy policy says that it is kept, encrypted, what it could open, and that removing it at
  Apple ends it. [operations.md](operations.md#apple-calendar)
- [ ] **Not done.** A change made here to an event with guests on an Apple Calendar is not sent,
  because iCloud would email the guests. The two copies then differ until the event next
  changes on the phone. [ADR 0041](adr/0041-apple-calendar-is-caldav-with-an-app-specific-password.md)
- [ ] **Not done.** A calendar with more than 10,000 events cannot be connected, and nothing on
  the page says why: it stays on "Bringing your plans over". [ADR 0041](adr/0041-apple-calendar-is-caldav-with-an-app-specific-password.md)
- [ ] **Not done.** The form does not limit how often a sign-in is tried. Apple counts wrong
  passwords, and an adult could type someone else's address.
  [dashboard.md](dashboard.md#a-connected-apple-calendar)

## Letters, invites and forwards

- [ ] **Not verified.** A letter sent from a phone to the server: a photo of one, a message
  forwarded in Telegram, and a PDF sent as a file. Tried only on drawn images, a written PDF and
  stand-in webhooks. [agent-and-tools.md](agent-and-tools.md#letters-invites-and-forwards)
- [ ] **Not done.** A day with no time in a letter (a day off school, a form due back) is a
  reminder the evening before, not an all-day event, so it is not in the calendar feed or in a
  connected calendar.
  [ADR 0039](adr/0039-a-letter-is-one-call-and-a-day-with-no-time-is-a-reminder.md)
- [ ] **Not done.** A scanned PDF, a Word file and a photo in Apple's HEIC format sent as a file
  are not read; the assistant says so and asks for a photo. Nothing can be emailed to it.
  [agent-and-tools.md](agent-and-tools.md#letters-invites-and-forwards)
- [ ] **Not done.** A letter is not shown to the model a second time, so a question about it two
  turns later is answered only from what was recorded.
  [ADR 0039](adr/0039-a-letter-is-one-call-and-a-day-with-no-time-is-a-reminder.md)
- [ ] **Not done.** Decide whether the guard on other people's words is enough. A letter, a
  forward or a file is somebody else's writing, read by the model as part of a family member's
  message. The most it can do is put wrong dates on that household's calendar, which one undo
  removes. [ADR 0039](adr/0039-a-letter-is-one-call-and-a-day-with-no-time-is-a-reminder.md)
- [ ] **Not done.** The privacy policy says that letters sent in hold other people's names, a
  school's and a clinic's details, that a PDF's words are kept with the message with no end date,
  and how a household has one removed. [data-model.md](data-model.md)
- [ ] **Not done.** A limit on how many files one household can have read in a day. One file is
  bounded (15 MB, 10 pages, 20 seconds); the number of files is not.
  [agent-and-tools.md](agent-and-tools.md#letters-invites-and-forwards)

## Spending from receipts

- [ ] **Not verified.** A real receipt keeps its shop, its total and the price of each line. Only
  drawn receipts of eight lines have been read: no weights, multi-buys, VAT lines or creases.
  [evals.md](evals.md#previous-full-run-spending-from-receipts)
- [ ] **Not verified.** "On food" and the split by kind of thing rest on the nightly sorting of
  items, and price changes on staples have not been checked against a real run of receipts.
  [agent-and-tools.md](agent-and-tools.md#spending-appservicesspendingpy)
- [ ] **Not verified.** The month's spending in the digest on a real Sunday. Tried with a pinned
  clock only. [architecture.md](architecture.md#scheduled-jobs)
- [ ] **Not done.** Spending is only what a receipt shows. Cash with no receipt, a takeaway or a
  bill cannot be added, so a month's figure is a floor.
  [ADR 0040](adr/0040-spending-is-kept-from-receipts-beside-the-stock-log.md)
- [ ] **Not done.** Setup does not ask for the currency: it is pounds until someone changes it on
  Settings or says so. A receipt from abroad is counted as if it were in the household's
  currency, a refund cannot be kept, and amounts always have two decimals.
  [dashboard.md](dashboard.md#spending)
- [ ] **Not done.** The month's spending goes to the family group with the Sunday digest. There
  is no way to keep it to one adult. [architecture.md](architecture.md#scheduled-jobs)
- [ ] **Not done.** Prices read from receipts are kept for good. The photos are deleted after
  `MEDIA_RETENTION_DAYS`; the amounts are not, and the privacy policy has to name them.
  [data-model.md](data-model.md#receipts-and-spending)

## For a public release

- [ ] **Not done.** Another household can sign up by itself. `/setup` closes after the first.
  [spec.md](spec.md#1-overview)
- [ ] **Not done.** What running many households needs: per-household limits, cost, support. The
  schema is ready and the product is not. [data-model.md](data-model.md)
- [ ] **Not done.** A privacy policy and terms, naming the model provider that reads messages and
  photos. [operations.md](operations.md#media-storage)
- [ ] **Not done.** A household can export and delete its data. Today an admin can download a
  copy, and nothing deletes. [dashboard.md](dashboard.md#system)
- [ ] **Not done.** The cost of one household for a month, for each provider. Token counts are
  stored and no figure is worked out. [operations.md](operations.md#cost-tracking)
- [ ] **Not done.** A support contact and a status page. Nothing is written yet.
- [ ] **Not done.** A licence for the public repository. There is no `LICENSE` file.
