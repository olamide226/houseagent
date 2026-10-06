# Channels

A channel is one adapter module in `app/channels/` plus identity rows. Telegram is built;
WhatsApp and iMessage are specified in [spec.md section 6](spec.md#6-channel-adapters).

## The adapter contract

`ChannelAdapter` (`app/channels/base.py`): `verify`, `parse`, `fetch_media`, `send_text`, `react`,
`send_template`, `dm_thread_id`, `format`, plus a `Capabilities` record. An adapter that cannot do
something raises `NotSupported` and the router degrades (an emoji as text instead of a reaction).
Adapters register in `ADAPTERS` only when their environment variables are set.

Every adapter must pass `tests/contract/test_adapters.py`: recorded webhook payloads for text,
voice, photo, location, reaction, group and own-message, parsed into golden `InboundEvent` JSON.
A new channel adds `tests/contract/fixtures/<channel>/` and a factory in that file.

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
