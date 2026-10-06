# 0027 iMessage threads, tapbacks and voice notes

## Context

The spec describes the BlueBubbles adapter in a paragraph and says to verify the paths against
the server's API. Reading the server source at v1.9.9 settled the paths and turned up five things
the paragraph does not cover. There is no Mac to test against, so each is a decision made from
the source, not from a running server.

## Decision

- **A DM's thread id is always `iMessage;-;{handle}`**, built from the sender, not copied from
  `data.chats[0].guid`. The router recognises a DM as a member's own only when its thread id
  equals `dm_thread_id(handle)`. A chat guid with another service prefix (`SMS;-;` for a text
  message) would make the router refuse to answer in it. A group's thread id is its chat guid.
- **Own messages are dropped in `parse`, with a 200**, like every other adapter. The spec says to
  reject them in verification; a 401 for a webhook that carried the right secret would look like
  a misconfiguration in the logs.
- **Tapbacks are six fixed kinds.** Inbound, each maps to one emoji; a tapback taken back, a
  sticker and anything unnamed is ignored. Outbound, the ack tick has no tapback, so with the
  Private API it is sent as `like`, and without it as a message containing the tick. An emoji
  with no tapback raises `NotSupported` and the router sends it as text.
- **CAF is converted in the pipeline, not the adapter**, and only when the download really is a
  CAF file (it starts with `caff`). BlueBubbles converts voice notes to MP3 itself when it can,
  and its webhook then already says `audio/mp3`. Conversion uses `ffmpeg` with the spec's
  arguments, in a thread.
- **`message-send-error` is read** as a failed delivery status, so a send the Mac gives up on
  later moves to the member's next channel ([ADR 0022](0022-permanent-failures-and-the-next-channel.md)).
  The spec's setup subscribes to `new-message` only; the docs ask for both events.
- **4xx is final, 5xx and silence are retried.** A 500 is what BlueBubbles answers when iMessage
  itself fails to send.

## Consequences

- A member who writes by text message (green bubble) is answered over iMessage. If they have no
  iMessage, that send fails and moves to their next channel.
- The assistant's ack on iMessage reads as a thumbs-up tapback or a tick message, depending on
  `BB_PRIVATE_API`. `Capabilities.ack_emoji` stays the tick the spec gives.
- A stored voice note is the WAV, about 32 kB per second, or the MP3 BlueBubbles made.
- A shared location has no coordinates: the card file is not parsed. Nothing uses them.
- All of this is unverified against a server. The fixtures in
  `tests/contract/fixtures/imessage/` are built from the serializer in the server source, in the
  form it uses for webhooks, with invented handles.
