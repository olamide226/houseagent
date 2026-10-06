# 0020 The WhatsApp 24-hour window is checked by the router, and the template carries the text outside it

## Context

WhatsApp delivers free-form messages only within 24 hours of the person's last message. Outside
that, only an approved template is delivered. The spec puts the check in the router
(`capabilities.proactive_window_hours`), computed from the thread's last inbound message, and
sends `WA_REMINDER_TEMPLATE` with the text as its one parameter. It leaves open what counts as
being heard from, what happens exactly at 24 hours, and how text is made to fit a parameter.

## Decision

- **The window is per thread and measured from `messages`**: the newest inbound row of that
  thread. A group has its own window, opened when someone writes in the group.
- **Connecting counts.** An invite code is never stored as a message, so for a DM the member's
  `channel_identities.verified_at` on that channel also counts. Otherwise "Hi Ola, you're
  connected" would go out as a reminder template.
- **At exactly 24 hours the window is closed.** A template is always deliverable; a free-form
  message a second too late is not.
- **Only text is wrapped.** An ack reaction is always an answer to a message that has just
  arrived, so it is sent as it is.
- **The adapter makes the text fit**: line breaks, tabs and runs of spaces become single spaces,
  which Meta requires of a parameter. The router cuts the text to 900 characters, ending in an
  ellipsis when it had to cut.
- **The template's name is a capability** (`Capabilities.proactive_template`, from
  `WA_REMINDER_TEMPLATE`, default `household_reminder`), sent in `en_GB`. An adapter that raises
  `NotSupported` for templates gets a plain send.
- **The message history keeps what was meant**: the `out` row holds the full text and
  `meta.template`.

## Consequences

- A reminder to someone who has not written for a day arrives as "Reminder from Home: ...", on one
  line, even when it is a login link or a morning brief. Briefs lose their line breaks.
- Templates are billed by Meta; free-form messages inside the window are not.
- The window is measured from when the webhook was stored, not when the message was sent. If Meta
  delivers a webhook late, the router can believe the window is open after it has closed. Meta
  then reports the send as failed (error 131047) and it moves to the member's next channel
  ([ADR 0022](0022-permanent-failures-and-the-next-channel.md)).
- The template body in the spec, `Reminder from Home: {{1}}`, ends with its variable, which Meta's
  review rejects. [channels.md](../channels.md#template-approval) gives a body that keeps text
  after the variable. Approval was not attempted: there is no Meta account.
- If the template is missing, paused or rejected, every send outside the window fails for good
  and moves to the next channel, or is lost for a member who has only WhatsApp.
