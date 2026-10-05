# 0006 MediaStore with S3 and ImgBB backends

## Context

Receipt and fridge photos must be stored long enough for the agent to read them. Some deployments
have an S3-compatible bucket; others want zero infrastructure.

## Decision

Media goes through a `MediaStore` interface (`put`, `get`, `delete`) with two backends chosen by
`MEDIA_BACKEND`: `s3` (private bucket, images, audio and documents) and `imgbb` (images only, one
API key, public to anyone holding the URL). The pipeline and the agent only ever see `MediaRef`.

## Consequences

- The storage choice is one environment variable.
- ImgBB is acceptable to start with; S3 is recommended once real receipts flow, since they can
  show an address and partial card details.
- **Not implemented yet.** Milestone 1 has no `MediaStore`: voice notes are transcribed in memory
  and only the transcript is kept, and photos are stored as references the agent cannot read.
