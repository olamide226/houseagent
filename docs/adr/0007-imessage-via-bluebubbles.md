# 0007 iMessage via BlueBubbles

## Context

Apple offers no bot API for iMessage. The options are a third-party relay service or a Mac the
household controls.

## Decision

Reach iMessage through a BlueBubbles server on an always-on Mac signed into a dedicated Apple ID,
reachable from the cluster over Tailscale. Tapbacks and threaded replies need its Private API and
are optional (`BB_PRIVATE_API`).

## Consequences

- Nothing on the Mac is public, and message content stays on hardware the household owns.
- The Mac is a single point of failure, so the worker will health-check it and fall back to each
  member's next channel.
- BlueBubbles field names shift between releases; the server version must be pinned.
- Built in milestone 6 against the server source at v1.9.9. What had to be decided then is in
  [ADR 0027](0027-imessage-threads-tapbacks-and-voice-notes.md) and
  [ADR 0028](0028-a-degraded-channel-and-the-next-identity.md).
