# 0021 A WhatsApp group is asked for, and becomes the primary thread when Meta confirms it

## Context

The spec's "Create WhatsApp group" button "calls the Groups API, stores the thread, sets it as
the household's primary thread, and shows the invite link and a QR code". Meta's Groups API (read
6 Oct 2026) creates a group asynchronously: `POST /{phone-number-id}/groups` documents no response
body, and the group id and invite link arrive in a `group_lifecycle_update` webhook. Nobody can be
added to a group; people join with the invite link. The API needs an Official Business Account
and allows eight participants.

## Decision

- The button inserts a thread `pending:{subject}` for the household and calls the API. If the
  call fails, the row is rolled back and the error is shown.
- A `group_create` webhook whose subject matches a pending thread gives that thread the real group
  id and makes it the household's primary thread. A failed one deletes the pending thread. A
  webhook for a subject nobody asked for, or a repeat, does nothing. If the response does carry an
  id, the same step runs at once.
- The subject is the correlation key because it is the one field both the request and the
  documented webhook carry. One group per subject can be pending at a time.
- The invite link is never stored. "Invite link" on the Channels page asks Meta for it each time
  and shows it with a QR code.
- Groups are created with Meta's default, `auto_approve`: anyone holding the link can join.
- Group creation is an optional adapter capability (`GroupHost`), not part of `ChannelAdapter`.

## Consequences

- The page shows "being created" until the webhook arrives; the app must be subscribed to the
  `group_lifecycle_update` field or it never will. "Forget" on the page drops a request that was
  never answered. If Meta did create that group, it then exists with nobody in it.
- A refusal that arrives by webhook is only logged (`group_not_created`); the page just stops
  showing the pending group.
- Someone who is not in the family but has the link can join and read what the agent says there.
  Their own messages are ignored, as any stranger's are. Meta can reset the link; the dashboard
  has no button for it.
- Two households asking for the same subject at the same moment would be refused the second
  request. With one household this cannot happen.
- Setting the primary thread is logged in Activity but not undoable; choose the other group to
  reverse it.
