# 0018 Media: stored before the turn, swept hourly, and optional

## Context

ADR 0006 chose the `MediaStore` interface with S3 and ImgBB backends. Building it raised four
questions the spec does not settle: what happens when no backend is configured, where in the
turn's transaction media is stored, how retention runs, and how ImgBB images are deleted.

## Decision

- **No backend, no photos.** `MEDIA_BACKEND` defaults to `s3`, but the store exists only when its
  variables are set. Without one, text and voice notes work as before (a voice note is transcribed
  in memory), and a photo reaches the agent as `[photo received; it could not be loaded]`. The same
  note covers a failed download or upload. A model without image input gets
  `[photo received; this model can't read photos]`. None of these fails the turn.
- **Media is stored before the turn's savepoint**, in the household transaction. If the turn then
  fails, its writes roll back but the storage reference stays on the message, so retention can
  still find and delete the object.
- **Retention is an hourly sweep**, not a 04:00 job. `media_cleanup` deletes stored media on
  messages older than `MEDIA_RETENTION_DAYS` and removes the storage fields from the message;
  text, captions and transcripts stay. Rows are claimed with `SKIP LOCKED` and a cleaned row no
  longer matches, so a second run or a second replica is harmless and no `job_runs` row is
  needed. If the store is unreachable the reference is kept for the next run.
- **ImgBB has no delete call.** Its API offers only a `delete_url` web page, so `delete()` is a
  no-op there and the upload's `expiration` (retention, capped at 180 days) removes the image. The
  sweep still clears the reference at the same age.
- **At most four photos per turn** reach the model; the turn says how many were left out.
- The sweep only handles media stored by the configured backend. After switching backends, what
  the old one holds is left for that bucket's own lifecycle rule.

## Consequences

- A deployment can run with no media infrastructure at all, at the cost of not reading photos.
- With `s3`, a bucket lifecycle rule is still worth setting as a backstop: an object whose upload
  succeeded in a transaction that later failed to commit is not referenced by any row.
- With `imgbb`, an image cannot be removed early, and anyone holding its URL can view it until it
  expires. Receipts can show an address or part of a card number; use `s3` for real ones.
- Retention is by message age in UTC and runs within an hour of the cutoff rather than at a fixed
  local time.
