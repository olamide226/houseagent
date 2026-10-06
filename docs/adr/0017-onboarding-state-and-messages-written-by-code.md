# 0017 Onboarding: one state per household, and two messages written by code

## Context

Spec section 12.2 describes onboarding as a conversation of seven steps tracked in
`households.onboarding_state`, with an extra prompt section and the `onboarding_advance` tool
while it is active. It leaves open how the conversation starts, how a second adult invited from
chat receives their invite ("adults get invited via CLI", but there is no CLI), and what the
`presence` step does before the presence endpoint exists.

## Decision

- **State.** `onboarding_state` is `{"step", "done", "skipped"}`. `step` is the first step not in
  `done`, in the spec's order, and `null` once all are done. The tool and the prompt section are
  offered only while `step` is not null, and a finished setup cannot be reopened by a stale call.
  The verbatim onboarding prompt names only the step, so one line per step is appended saying
  what to ask and how the answer is recorded.
- **The first question is sent by code.** When an adult redeems an invite while the `family` step
  is open, the "you're connected" reply also asks who lives here. Anyone who connects later gets a
  one-line welcome instead. No model call is involved in either.
- **An adult added in chat gets an invite written by code.** `add_family_member` with role adult
  creates the member and an invite, and queues a separate message to the person who asked, holding
  the deep link and the code for them to pass on. The tool result only says that this happened.
  Asking again for an adult who has not connected sends a fresh invite, which replaces the old one.
- **Children** have no invite, chat, quiet hours or login.
- **Connecting is logged** as an `invite.redeem` action. It shows in Activity, and because it
  touches the member's row, "undo" of the action that added them is refused from then on.
- **`presence` is not a step yet.** It needs the Shortcut endpoint of milestone 5 and joins
  `ONBOARDING_STEPS` then. Setup currently ends after `rhythm`.

## Consequences

- The second adult can be set up with no SQL: from chat, or from the Family page.
- The invite code never passes through the model, the tool result or `agent_actions`. It does
  exist in plain text in the one outbound message that carries it (`outbox.text` and the `out`
  row in `messages`), for as long as that history is kept; only its hash is on the member. The
  same is already true of dashboard login links. A code works once per channel and for 7 days.
- Any connected adult can invite another adult. That matches the dashboard, where every adult can
  edit everything.
- A household that finished setup before milestone 5 will not be asked the presence step unless
  that milestone reopens it.
- If two adults connect before the family question is answered, both are asked it.
