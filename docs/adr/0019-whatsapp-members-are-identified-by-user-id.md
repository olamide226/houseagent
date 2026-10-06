# 0019 A WhatsApp member is identified by the business-scoped user id

## Context

The spec says a WhatsApp handle is the `from` phone number in E.164. Since April 2026 Meta sends a
business-scoped user id (BSUID, such as `GB.13491208655302741918`) as `from_user_id` on every
inbound message, and leaves `from` and `wa_id` out when the sender has a WhatsApp username and
has not interacted with the business number for 30 days. Sends can address a BSUID with the
`recipient` field. (Meta's reference, read 6 Oct 2026.)

If the phone number were the handle, a member with a username who came back from a month away
would arrive with no number, match no row in `channel_identities`, and be ignored as a stranger.

## Decision

- The handle is `from_user_id`. Only a payload without one falls back to the phone number in
  E.164 (`+447700900101`).
- A DM thread's external id is the handle. Sends use `recipient` for a user id, `to` for a phone
  number, and `recipient_type: "group"` with the group id for a group. The adapter tells the
  three apart by shape.
- No phone number is stored for someone identified by user id.

## Consequences

- Identity is stable whether or not Meta includes the number.
- `channel_identities.handle` for WhatsApp is usually not a phone number, so the dashboard cannot
  show one and nobody can be pre-linked by number: connecting is always by invite code.
- The user id is scoped to the business portfolio. Moving the number to another portfolio would
  give everyone a new id, and each person would connect again with a new invite.
- If Meta ever sent a message without `from_user_id` to someone linked by user id, they would be
  treated as a stranger. Meta documents the field as always present.
- Never exercised against Meta: there are no WhatsApp credentials. The contract fixtures are built
  from the documented shapes, including one with no phone number.
