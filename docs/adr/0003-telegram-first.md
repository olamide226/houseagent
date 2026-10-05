# 0003 Telegram first

## Context

Three channels are planned: Telegram, WhatsApp and iMessage. WhatsApp needs business verification,
an approved template and a dedicated number. iMessage needs an always-on Mac running BlueBubbles.

## Decision

Build Telegram first. It is free, needs no verification, and supports groups, voice notes and
reactions natively. The adapter contract and its fixture-based test suite are written against
Telegram; the other channels must pass the same suite.

## Consequences

- The household can use the system from milestone 1.
- The bot must have privacy mode disabled and be a group admin to see messages and reactions.
- Channel-specific features (the WhatsApp 24-hour window, BlueBubbles health) are deferred to the
  milestones that add those channels.
