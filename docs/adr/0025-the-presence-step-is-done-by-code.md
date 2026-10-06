# 0025 The presence step of setup is done by code

## Context

The last onboarding step in spec section 12.2 is `presence`: "DMs each adult their Shortcut URL
and steps", with nothing to ask and nothing to write. But the model has no tool that sends a
direct message, the URL contains a secret that should not pass through the model, and the token
behind it has to be written somewhere because only its hash is kept. ADR 0017 left the step out
until the endpoint existed.

## Decision

- `presence` is the seventh step, and **code completes it**. When a call to `onboarding_advance`
  leaves it as the only step open, or names it, each connected adult who has no link yet gets a
  new token and a private message with the URL and the phone steps. The tool result tells the
  model only that this happened and to whom.
- **Skipping works both ways.** `onboarding_advance(presence, skipped=true)` before the end sends
  nothing, and asking for it early sends the links at once.
- **A link someone already has is never replaced** by setup or by connecting. A new token would
  silently break the automations on their phone. Replacing is a deliberate click on Settings.
- **An adult who connects after setup sent the links** gets the one-line welcome and then their
  own link, as the spec says for the second adult. If setup skipped the step, or the household
  was set up before this milestone, they get the welcome only.
- The message to whoever is talking to the assistant ignores quiet hours; the other adults' waits
  for theirs.
- If a household is somehow left at `presence` as its current step, the step's guide line tells
  the model to call the tool, which finishes it.

## Consequences

- Setup ends in the same turn as the last answer. The model never sees or repeats a token, and
  the token is in no tool result and no `agent_actions` row.
- The link is in plain text in the one outbound message that carries it (`outbox`, `messages`),
  like invite codes and login links.
- Undoing the last step reopens it but takes nobody's link away. Finishing it again sends nothing
  to people who already have one.
- "Skip the rest" at an earlier step can still end with the links being sent, if the model skips
  the other steps one by one. The message is one optional paragraph.
