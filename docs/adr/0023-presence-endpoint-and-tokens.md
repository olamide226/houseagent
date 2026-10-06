# 0023 The presence endpoint: personal tokens, always 204, limits kept in the database

## Context

Spec section 11 gives the endpoint: `POST /presence/{token}` with an event and a place name, a
token per adult stored as a SHA-256 hash, "204 always", 30 calls per token per hour, and unknown
place names becoming places of kind `other`. It leaves open where the rate limit lives, what
"always" covers, how the `Home` place comes to exist, and how a URL that cannot be read back from
its hash is "shown" on Settings.

## Decision

- **The token** is `secrets.token_urlsafe(32)`. Only its hash is stored, so a link is shown once,
  when it is made: in the setup message, or on Settings after "Make link" or "Replace link". The
  page otherwise shows only whether each adult has one. Children never have a working token.
- **204 for everything**: a wrong token, a body that is not JSON, an unknown event word, a place
  name over 80 characters, the rate limit, and a failure while handling a valid ping. A failure
  only a valid token can cause would otherwise tell a stranger the token is real. It is logged.
- **The rate limit is a count of `presence_events`** for that member in the last hour, not a
  counter in the api process. A dropped ping stores nothing, so the count cannot run away.
- **No household lock.** A turn holds the household's advisory lock while the model thinks, and a
  phone's call must not wait for that. The rules are made safe for concurrent calls one statement
  at a time instead (ADR 0024).
- **Places** are matched by name whatever the case or spacing. An unknown name is added with kind
  `other`, except a place called Home, which is `home`: nothing else creates the home place, and
  the "out and about" rule needs one. Settings lists places, changes their kind, and adds them.
- **Tokens stay out of the access log.** uvicorn logs each request's path. The token part of
  `/presence/`, `/login/` and `/ics/` paths, and `token=` and `secret=` query values, are masked.

## Consequences

- Rotating a link breaks the automations on that phone until they are given the new one. The page
  says so.
- A limit kept in the database works across api replicas and restarts. Thirty rows an hour per
  adult is also the most a leaked token can write; it can add up to thirty junk places an hour.
- There is no way to tell a caller their body was malformed. Debugging a Shortcut means reading
  the logs, where `presence_ignored` says why.
- A token in a URL can still be seen by anything between the phone and the api that logs URLs:
  the ingress in front of it needs the same care.
- A place cannot be renamed or removed from the dashboard yet.
