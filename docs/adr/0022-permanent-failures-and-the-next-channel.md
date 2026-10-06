# 0022 A send that fails for good is tried once on the member's next channel

## Context

The spec says a failed send backs off five times, then is marked `failed` and tried on "the
member's next channel once". With one channel that could not be built. Two things needed deciding
with WhatsApp: some failures cannot succeed on a retry, and WhatsApp accepts most sends and
reports failure later, in a status webhook.

## Decision

- **`PermanentError`** is a `ChannelError` the router does not retry. Both adapters raise it for a
  4xx response that is not throttling: a blocked bot, an unknown chat, an invalid recipient, an
  expired token.
- **A send has failed for good** when it raises `PermanentError`, when its fifth retry fails, or
  when the channel later reports it failed. For the last, the api process matches a WhatsApp
  `failed` status to the `sent` outbox row by channel and message id and marks it `failed`.
- **Then the fallback**: a new outbox row addressed to the member's DM on their next connected
  channel, in the order Telegram, WhatsApp, iMessage, with the same text, urgency and quiet-hours
  flag, and the dedupe key `fallback:{original id}`.
- **Once.** That key means one fallback per send, even if the original is retried from the
  dashboard, and a row with that key never falls back itself.
- **Only a text to one person moves.** A reaction is not sent to another chat, and a group send
  has no next channel. A reply in a DM does move.
- Other statuses (`sent`, `delivered`, `read`) are parsed and ignored: `outbox` has no column for
  them.

## Consequences

- An expired WhatsApp token or a blocked Telegram bot no longer loses reminders for anyone with a
  second channel.
- The fallback goes through the same destination check as any send, so it can only reach a
  verified handle of that member. The allowlist property test runs with one channel refusing
  everything.
- A fallback can deliver something the first channel did deliver, if the provider reports a
  failure wrongly. Then the member sees it twice.
- After a failure reported by status, the thread history still holds the message as said.
- `outbox.channel_used` is now also set on a failed row, to the channel that was tried. The
  Channels page counts those.
- A member with one channel gets nothing: the row stays `failed` and shows in Activity for retry.
