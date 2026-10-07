# Channels

A channel is one adapter module in `app/channels/` plus identity rows. Telegram, WhatsApp and
iMessage are built. Only Telegram's wire format has been checked against the service itself;
WhatsApp and iMessage have been tested against recorded payloads and mocked calls.

## The adapter contract

`ChannelAdapter` (`app/channels/base.py`): `verify`, `parse`, `parse_updates`, `fetch_media`,
`send_text`, `react`, `send_template`, `dm_thread_id`, `format`, plus a `Capabilities` record.

- `parse` returns the messages in a webhook. `parse_updates` returns what else it carried:
  delivery statuses and the outcome of a group creation. Telegram has neither.
- An adapter that cannot do something raises `NotSupported` and the router degrades (an emoji as
  text instead of a reaction, a plain send instead of a template).
- A failed send raises `ChannelError`, which the router retries, or `PermanentError`, which it
  does not ([ADR 0022](adr/0022-permanent-failures-and-the-next-channel.md)).
- `GroupHost` is an optional second protocol for a channel that can create a group and hand out
  its invite link. Only WhatsApp implements it.
- `HealthChecked` is an optional third: a channel that runs on a server of ours has `ping()` and
  a `degraded` flag. Only iMessage implements it ([below](#when-bluebubbles-does-not-answer)).

Adapters register in `ADAPTERS` only when their environment variables are set.

Every adapter must pass `tests/contract/test_adapters.py`: recorded webhook payloads for text,
voice, photo, location, reaction, group and own-message, parsed into golden `InboundEvent` JSON.
Any further payload a channel records is checked the same way. A new channel adds
`tests/contract/fixtures/<channel>/` and a factory in that file.

## Telegram

### Setup checklist

1. Create the bot with BotFather. Put the token in `TG_BOT_TOKEN` and the username in `TG_BOT_USERNAME`.
2. BotFather `/setprivacy` then `Disable`, so the bot sees every message in the family group.
3. Choose a random `TG_WEBHOOK_SECRET` and register the webhook:

   ```sh
   curl "https://api.telegram.org/bot$TG_BOT_TOKEN/setWebhook" \
     -d "url=$PUBLIC_BASE_URL/webhooks/telegram" \
     -d "secret_token=$TG_WEBHOOK_SECRET" \
     -d 'allowed_updates=["message","edited_message","message_reaction"]'
   ```

4. Add the bot to the family group and make it an admin, so reaction updates arrive. The first
   message there from a connected member makes it the household's primary thread.

### Payload notes

- Verification: header `X-Telegram-Bot-Api-Secret-Token`, compared in constant time.
- `chat.id` is the thread; `private` is a DM, `group` and `supergroup` are a group. `from.id` is the handle.
- `text` or `caption` is the text. `voice.file_id` is audio; the largest `photo` size is the image; `location` gives lat/lng.
- `message_reaction` becomes an event with `reaction_emoji`. It has no message id of its own, so
  its id is `reaction:{update_id}`, which keeps provider retries idempotent. A removed reaction is ignored.
- `edited_message` is ignored.
- `/start CODE` is treated as the invite code, so the deep link `https://t.me/<bot>?start=<code>` connects in one tap.
- The bot's own messages are dropped: the bot's user id is the part of the token before the colon.
- Sends use `parse_mode=HTML`; `format()` escapes `&`, `<`, `>`. The ack reaction is a thumbs-up.

### Known limits

- Voice notes need `STT_*` configured; without it the agent sees `[voice note] (could not be transcribed)`.
- Photos need a media backend (`MEDIA_BACKEND` with its variables) and a model with image input.
  Without either the agent is told a photo arrived and that it could not read it. Telegram sends a
  photo's caption as the message text, so `MediaRef.caption` is not set.
- At most four photos from one batch of messages reach the model.
- Invite attempts are limited to 5 per handle per hour, counted in the api process's memory, so
  the count resets on restart.

## WhatsApp

Built against Meta's Cloud API reference as read on 6 Oct 2026 (Graph API v26.0). **It has never
talked to Meta**: there is no WhatsApp account, so everything below is tested against recorded
payloads and mocked calls.

### Setup checklist

1. In Meta's developer console create a Business app with the WhatsApp product. Use a dedicated
   phone number, not one that is on the WhatsApp Business app.
2. Create a system user with a permanent token that has `whatsapp_business_messaging` and
   `whatsapp_business_management`. Set `WA_ACCESS_TOKEN` to it, `WA_PHONE_NUMBER_ID` to the
   number's id and `WA_APP_SECRET` to the app secret (App settings, Basic).
3. Choose a random `WA_VERIFY_TOKEN`. In the app's WhatsApp configuration set the callback URL to
   `{PUBLIC_BASE_URL}/webhooks/whatsapp` and the verify token to the same value. Meta calls the URL
   once to check it; the api must be running.
4. Subscribe the app to the webhook fields `messages` and `group_lifecycle_update`.
5. Get the reminder template approved (below).
6. Each adult sends their invite code, from the Family page, to the business number in a DM.
7. Optional: create the family group on the dashboard's Chat apps page.

`WA_API_VERSION` defaults to `v26.0` and `WA_REMINDER_TEMPLATE` to `household_reminder`.

### Template approval

WhatsApp only delivers free-form messages within 24 hours of the person's last message. Outside
that window the router sends the template named by `WA_REMINDER_TEMPLATE`, with the text as its
one parameter ([ADR 0020](adr/0020-whatsapp-window-and-template.md)). Approval happens in Meta's
console and is not automated.

1. WhatsApp Manager, Message templates, Create template.
2. Category **Utility**, name `household_reminder`, language **English (UK)**. The adapter sends
   `en_GB`; a template in another language will not match.
3. No header, no footer, no buttons. Body, with one variable and text after it:

   ```text
   Reminder from Home: {{1}} (reply to this message to answer)
   ```

   The spec's body, `Reminder from Home: {{1}}`, ends with its variable, and Meta rejects a body
   that starts or ends with one.
4. Give a sample for the variable, such as `GP for Ada tomorrow at 10:30, Hurley Clinic`.
5. Submit. Review can take up to 24 hours. The status must be Approved (shown as Active) before
   anything outside the window is delivered.

If it is rejected, or later paused or disabled, see
[operations.md](operations.md#whatsapp-template-rejected-or-paused).

### Payload notes

- **Subscription check:** `GET /webhooks/whatsapp` answers `hub.challenge` as plain text when
  `hub.mode` is `subscribe` and `hub.verify_token` matches, else 403.
- **Verification:** `X-Hub-Signature-256` must equal `sha256=` plus the hex HMAC-SHA256 of the raw
  body under `WA_APP_SECRET`, compared in constant time.
- **Who:** the handle is `from_user_id`, the business-scoped user id Meta sends on every message.
  The phone number in E.164 is used only when there is no user id; Meta leaves the number out for
  someone with a username who has been quiet for 30 days
  ([ADR 0019](adr/0019-whatsapp-members-are-identified-by-user-id.md)).
- **Where:** a message with `group_id` is in that group; otherwise the thread is the sender's DM.
- **What:** `text.body`; `interactive.button_reply.title`; `audio`, `image` and `document` by
  media id, with a caption as the message text; `location`; `reaction` (a removed reaction is
  ignored); `context.id` is the message being replied to. Stickers, contact cards and unsupported
  types are dropped.
- **Own messages:** only changes of the `messages` field are read, so `smb_message_echoes` is
  ignored, and a message whose `from` is the business number is dropped.
- **Statuses:** a `failed` status marks the matching send failed and moves it to the member's next
  channel. `sent`, `delivered` and `read` are ignored.
- **Media:** `GET /{version}/{media_id}` returns a URL valid for five minutes; it is downloaded
  with the same bearer token.
- **Sends:** `recipient` for a user id, `to` for a phone number, `recipient_type: "group"` for a
  group. A reply quotes with `context.message_id`. The ack reaction is a tick.
- **Groups:** `POST /{phone-number-id}/groups` only asks for a group. Its id arrives later in a
  `group_lifecycle_update` webhook, which is when it becomes the family's main chat
  ([ADR 0021](adr/0021-whatsapp-groups-are-created-asynchronously.md)).

### Known limits

- Meta's Groups API needs an Official Business Account, allows eight participants in a group, and
  cannot add anyone: each adult joins with the invite link. Anyone holding the link can join.
- A new group's window is closed until someone writes in it, so the first household messages
  there arrive as templates.
- Text sent by template is one line of at most 900 characters.
- The invite pages show a Telegram link and the raw code. There is no WhatsApp link because the
  business number is not in the configuration: tell people which number to message.
- Edited and deleted messages, and message types not listed above, are not read.
- `channel_identities.handle` is a user id, so nobody can be linked by phone number in advance.

## iMessage

Apple has no bot API, so iMessage goes through a [BlueBubbles](https://bluebubbles.app) server on
a Mac the household owns ([ADR 0007](adr/0007-imessage-via-bluebubbles.md)). The adapter was
written against the BlueBubbles server source at **v1.9.9** (read on 6 Oct 2026); pin that
version on the Mac. **It has never talked to a BlueBubbles server**: there is no Mac set up, so
everything below is tested against payloads built from that source and against mocked calls.

### Setup checklist

1. An always-on Mac signed into a dedicated Apple ID (the assistant's own, not a family
   member's), with BlueBubbles Server installed and its server password set. Turn off sleep.
2. Put the Mac and the cluster on the same tailnet. Nothing on the Mac needs to be public.
   `BB_BASE_URL` is the Mac's tailnet address and BlueBubbles port, for example
   `http://mac-mini.tailnet:1234`; `BB_PASSWORD` is the server password.
3. Choose a random `BB_WEBHOOK_SECRET`. In BlueBubbles, API and Webhooks, add a webhook to
   `{PUBLIC_BASE_URL}/webhooks/imessage?secret={BB_WEBHOOK_SECRET}` for the events **New
   Messages** (`new-message`) and **Message Send Errors** (`message-send-error`).
4. Optional: install the BlueBubbles Private API helper and set `BB_PRIVATE_API=true`. That turns
   the ack into a tapback and makes replies threaded. It needs System Integrity Protection
   disabled on the Mac, which is why it is off by default.
5. Each adult sends their invite code, from the Family page, to the assistant's Apple ID in a DM.
6. For a family group, add the assistant's Apple ID to it and write something there.

All three of `BB_BASE_URL`, `BB_PASSWORD` and `BB_WEBHOOK_SECRET` must be set for the channel to
be on.

### Payload notes

- **Verification:** BlueBubbles does not sign what it sends, so the `secret` query parameter must
  equal `BB_WEBHOOK_SECRET`, compared in constant time; anything else gets 401. The api's access
  log masks the parameter.
- **Shape:** a webhook body is `{"type": ..., "data": ...}`. Only `new-message` is read as a
  message. `data` is the message in the server's notification form.
- **Own messages:** `data.isFromMe` is dropped, so the assistant never answers itself.
- **Who:** `data.handle.address`, a phone number in E.164 or an Apple ID email address.
- **Where:** a `data.chats[0].guid` containing `;+;` (`iMessage;+;chat...`) is a group and that
  guid is the thread. Anything else is a DM, and its thread is always `iMessage;-;{handle}`, whatever
  prefix the server reports ([ADR 0027](adr/0027-imessage-threads-tapbacks-and-voice-notes.md)).
- **What:** `data.text` with the attachment placeholder character removed; each of
  `data.attachments` by `guid`, typed from `mimeType`; `threadOriginatorGuid` is the message
  being replied to.
- **Tapbacks** arrive as messages with `associatedMessageType` (`love`, `like`, `dislike`,
  `laugh`, `emphasize`, `question`) and `associatedMessageGuid` (`p:0/<guid>`). They become
  reactions with the matching emoji. A tapback taken back (`-like`), a sticker and any other kind
  are ignored, and so is the "Liked ..." text.
- **Voice notes** are recorded as `.caf`. BlueBubbles converts them to MP3 when it can; when the
  download is still a CAF file the pipeline converts it with
  `ffmpeg -i in.caf -ar 16000 out.wav` before transcription. `ffmpeg` is in the image.
- **Photos:** BlueBubbles converts HEIC to JPEG on download, and the download's content type is
  what the photo is stored as.
- **A shared location** is a small card file (`CL.loc.vcf`). It becomes a location with no
  coordinates: they are inside the file, which is not parsed.
- **Media:** `GET {BB_BASE_URL}/api/v1/attachment/{guid}/download?password=...`.
- **Sends:** `POST /api/v1/message/text?password=...` with `chatGuid`, `message`, a fresh
  `tempGuid` and `method` (`apple-script`, or `private-api` when `BB_PRIVATE_API` is on, which
  also adds `selectedMessageGuid` for a threaded reply). The answer's `data.guid` is kept.
- **The ack:** with the Private API, a `like` tapback through `POST /api/v1/message/react`;
  without it, a tick sent as a message of its own. iMessage has six tapbacks and no tick.
- **Failures:** a 4xx (unknown chat, wrong password) is final; a 5xx or no answer is retried. A
  `message-send-error` webhook marks the matching send failed and moves it to the member's next
  channel, as a WhatsApp `failed` status does.

### When BlueBubbles does not answer

The worker calls `GET /api/v1/ping` every five minutes (`imessage_health`). One failed ping marks
the adapter degraded; one good ping clears it. While it is degraded:

- A message for one person goes to their next channel, in the order Telegram, WhatsApp. That
  covers reminders, the arrival list, login links and a reply that was still waiting for an
  iMessage DM. Their `preferred_channel` is not changed, so their DMs return by themselves.
- A message for the whole household goes to each adult instead of an iMessage family group.
- Someone whose only channel is iMessage is still tried there, and so is an ack and anything
  addressed to an iMessage group. Those are retried with the usual backoff and fail after about
  43 minutes.
- The admin of each household that uses iMessage gets one message per outage, on a channel that
  works, and the Chat apps page says since when iMessage has been unreachable.

Nothing sent this way can reach anyone new: the fallback goes through the same destination check
as every send. What to do about an outage is in
[operations.md](operations.md#bluebubbles-outage).

### Known limits

- Never run against a real BlueBubbles server or a real iPhone.
- A message that only renames a group or adds someone is ignored.
- An edited or unsent message is not read (`updated-message` events are ignored).
- Text and iMessage use the same Apple ID; a text message (green bubble) from a member is
  answered over iMessage.
- Up to five minutes pass before an outage is noticed. Sends in that time fail and are retried.
- BlueBubbles posts each webhook once and does not retry. A message sent while the api is down
  or unreachable from the Mac is lost to the assistant and has to be said again.
- The invite pages show a Telegram link and the raw code. Tell people which Apple ID to message.
