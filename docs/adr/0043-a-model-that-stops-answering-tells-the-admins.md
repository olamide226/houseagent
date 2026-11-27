# 0043 A model that stops answering tells the admins, once, and the family is told why

## Context

From 23 to 26 November 2026 the trial server could not answer anyone. Anthropic had started
refusing its key (`401 authentication_error: invalid x-api-key`), every turn raised `LLMError` in
`app/llm/anthropic.py`, and every member got "Sorry, that didn't go through, try again?". Reminders
and the morning brief need no model, so they kept going and nothing looked wrong. The owner found
out by noticing that the bot never replied. The System page would have said why, for the newest
failed message only, but nobody had a reason to open it.

## Decision

- **Each adapter says what kind of failure it was.** `LLMError` carries a `cause` and the
  provider's own name for the error (`error_type`: an error type or a code, never its message).
  A cause is either the admin's to fix (`config`: a refused or missing key, no permission, no
  credit, a model the service does not have, a subscription CLI signed out or not installed) or
  one that passes (`transient`: a timeout, a network or 5xx failure, a 429 or an overloaded
  service, a subscription's usage limit). The Anthropic adapter goes by the API's error type, then
  the status; the OpenAI-compatible one by status, with a 429 whose code says the quota is gone
  counted as no credit; the CLIs by what they print, as before. An error nobody recognised counts
  as one that passes.
- **One outage for the installation.** The model and its key are the installation's, so
  `model_outages` holds one open row while turns fail, whichever household's they are. A partial
  unique index on the open row, and claiming the alert with one `update ... where alerted_at is
  null`, make two workers or a restart share it and tell nobody twice.
- **Who is told, and when.** Every admin of every household gets one message in their own chat
  through the outbox, in fixed words, so the warning needs no model. A `config` failure tells them
  on the first failed turn; a `transient` one only once at least two turns have failed and the
  first was 30 minutes ago, with no turn answered between. While it lasts they are told again at
  most once a day, on a turn that failed again: a reminder only goes out when someone was in fact
  not answered. The first turn that works closes the outage and tells each admin whose warning was
  delivered "I'm answering again"; a warning still held for quiet hours is cancelled instead. A
  passing failure that never told anyone is deleted when it ends, so the history holds outages,
  not blips.
- **Quiet hours are kept.** Nothing is lost by waiting till morning: messages are kept, reminders
  still go, and a person who writes in the night is told why there is no answer. Waking the admin
  would not make the fix any sooner.
- **The family is told why.** Once the admins have been told, a failed turn's reply says "Sorry, I
  can't answer right now: the AI service I use has stopped working. I've let Ola know." An admin
  in their own chat is given the cause itself. Before that, it is the usual apology.
- **Nothing secret is kept or shown.** A provider's message about a refused key can hold part of
  the key (OpenAI's `sk-proj-abcd****wxyz`, DeepSeek's `****wxyz`). Adapters take out the key they
  were given and anything shaped like a key before raising; a model failure is logged without its
  traceback, which would carry the provider's words.

## Consequences

- The System page's Model card leads with the model's health: answering and when it last did, or
  not answering since when and why, what the provider called the error, and the last five outages.
- Nothing is checked ahead of time. An outage begins with the first turn that fails, and a key
  that is refused at 3 a.m. while nobody writes is only noticed when somebody does.
- Messages that failed during an outage are not answered later; the "answering again" message says
  so.
- If an outage that told the admins it would pass turns into one they must fix, they are not told
  a second time until the daily reminder, which then names the new cause.
- Moving to another provider when one fails is not done; it is a possible follow-up.
