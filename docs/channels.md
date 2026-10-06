# Channels

A channel is one adapter module in `app/channels/` plus identity rows. Telegram and WhatsApp are
built; iMessage is specified in [spec.md section 6](spec.md#6-channel-adapters).

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
7. Optional: create the family group on the dashboard's Channels page.

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
