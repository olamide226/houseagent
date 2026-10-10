# Go-live checklist

What is still open before this is offered to the public. One household uses it today on one
server; nothing below stops that. Last checked against the code and the docs on 10 Oct 2026.

Each line has a status and a pointer to where the detail is.

- **Not done**: something has to be built, bought, decided or written.
- **Not verified**: it is built, and has never been tried against the real thing.
- **Done**: tick the box, change the status and name the pull request. Leave the line in place.

A pull request that opens or closes an item changes this page in the same change. Where a line
points to a "build record", the detail is in the notes kept while that part was built. Those are
not in this repository, so the line itself is all there is here.

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
- [ ] **Not verified.** A voice note sent from a phone to the server. The transcriber has only
  been given a sample file. [llm.md](llm.md#speech-to-text)
- [ ] **Not done.** Photos kept in a private S3-compatible bucket with a lifecycle rule. ImgBB
  links can be opened by anyone who has them. [operations.md](operations.md#media-storage)
- [ ] **Not verified.** A voice note with three changes is acknowledged within 10 seconds (p95).
  Never measured. [spec.md](spec.md#16-testing-and-acceptance-criteria)
- [ ] **Not verified.** A calendar app subscribed to the calendar feed.
  [dashboard.md](dashboard.md#calendar-feed)
- [ ] **Not verified.** A login link in a real Telegram chat is no longer spent by the preview.
  [ADR 0035](adr/0035-a-login-link-is-spent-by-its-button.md)
- [ ] **Not verified.** The Helm chart installed on a cluster. Linted and rendered only.
  [operations.md](operations.md#deploying-with-helm)
- [ ] **Not verified.** A subscription provider in a signed-in container, and what happens when
  its sign-in expires. [operations.md](operations.md#not-covered)
- [ ] **Not done.** Decide whether a public product may answer on a Claude or ChatGPT
  subscription. The vendors' terms are unclear, and OpenAI's were not read on the page.
  [llm.md](llm.md#what-the-vendors-terms-say)

## Quality and operations

- [ ] **Not done.** Evals run nightly in CI. Today they are run by hand.
  [evals.md](evals.md#running)
- [ ] **Not done.** Evals pass on a second model with an API key, several samples a case. The bar
  is met by one model on two wire formats. [evals.md](evals.md#latest-results)
- [ ] **Not done.** Choose the subscription providers' models. The small ones scored 58 and 59 of
  61 in one run. [evals.md](evals.md#the-subscription-providers)
- [ ] **Not done.** The server's database is backed up to another machine on a schedule.
  [operations.md](operations.md#backup-and-restore)
- [ ] **Not done.** A restore from such a backup is practised. One restore was done, when the
  household moved on 9 Oct 2026. [operations.md](operations.md#not-covered)
- [ ] **Not done.** CI builds the image and publishes it.
  [operations.md](operations.md#deploying-with-helm)
- [ ] **Not verified.** The server comes back by itself after a reboot.
  [operations.md](operations.md#on-one-server)
- [ ] **Not done.** Login, shop and calendar tokens are kept out of the shared proxy's access log.
  [operations.md](operations.md#logs)
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
- [ ] **Not done.** Dates and times follow one UK style, not the household's locale. Build
  record: dashboard redesign.
- [ ] **Not done.** The list at the shop is iPhone only. There is nothing for Android.
  [presence.md](presence.md#what-apple-allows)
- [ ] **Not done.** Telegram replies show stray stars and backticks, because text is sent escaped.
  [channels.md](channels.md#payload-notes)
- [ ] **Not done.** Telegram audio files and video notes are not read, only voice messages.
  [channels.md](channels.md#payload-notes)
- [ ] **Not done.** WhatsApp audio in AMR or AAC is not converted, so it is not transcribed.
  Build record: voice notes and the presence link.
- [ ] **Not done.** A reminder cannot be snoozed. No pull request for it was open on 10 Oct 2026.
  [data-model.md](data-model.md#calendar-rows)
- [ ] **Not done.** There are no all-day events. Build record: milestone 2.
- [ ] **Not done.** A receipt can still add an item beside its twin ("coconut milk", "semi skimmed
  milk"). [ADR 0031](adr/0031-rules-the-model-kept-breaking.md)

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
