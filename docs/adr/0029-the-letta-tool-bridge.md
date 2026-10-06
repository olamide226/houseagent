# 0029 The Letta tool bridge runs on the turn's own transaction

## Context

Spec section 8.4: Letta's custom tools `POST {PUBLIC_BASE_URL}/internal/tools/{name}` with a
bearer token, and "the API resolves `member_id` and `message_id` from the household's in-flight
turn". The spec pictures tools that each commit on their own. This codebase runs a whole turn in
one transaction that also holds the household's lock ([ADR 0009](0009-one-transaction-per-turn.md)),
and chat turns run in the worker while the api serves HTTP.

A bridge in the api that opened its own transaction would not work here. The turn's message row
is locked by the turn, so a tool's write that refers to it would wait for the turn to end, while
the turn waits for the tool. Even without that, writes made outside the turn would survive a
failed turn, could not be dry-run in the Playground, and would not be serialised with the turn.

Letta itself had moved by the time this was built. Its documentation describes a harness with a
WebSocket App Server and tools the client executes; the REST API that `letta-client` wraps is
documented for Letta Cloud only, and the self-hosted server that serves it ends at Docker tag
0.16.8.

## Decision

- **The bridge executes on the turn in flight.** Each process keeps a small registry,
  household id to the running turn's context. `POST /internal/tools/{name}` looks the household
  up there and runs the tool through the same `run_tool` the loop uses, on the turn's connection,
  one call at a time. The caller supplies only the household id and the arguments.
- **The process that runs the turn serves the bridge.** The api has the route for Playground
  turns. Under the Letta runtime the worker also listens, on the port of `WORKER_INTERNAL_URL`,
  with only this route. Each process tells Letta its own address by rewriting the agent's tool
  environment before the turn.
- **Authentication is the bearer token alone**, compared in constant time, checked before the
  body is read. No token configured means the route answers 404.
- **No turn in flight means 409 and no write.** That covers a call that arrives after a timeout,
  a retry, and a call for another household.
- **The REST API and `letta-client`** are used, as the spec and the brief ask, against the last
  self-hosted server that serves that API.
- **`LETTA_MODEL`, `INTERNAL_BASE_URL` and `WORKER_INTERNAL_URL` are settings the spec does not
  list.** Letta names models by its own handles, and `/internal` is by definition not at
  `PUBLIC_BASE_URL` once an ingress allowlist is in front of it.

## Consequences

- A tool called through Letta is indistinguishable in the database from one called by the loop,
  and the eval suite runs unchanged through either runtime.
- The worker listens on a port, but only under `AGENT_RUNTIME=letta`. The loop deployment is
  the spec's: one port, on the api.
- The api and the worker still share nothing but Postgres. Letta talks to each separately.
- Anyone who holds the token and can reach `/internal` can run a tool as the member whose turn
  is in flight, for as long as it is. The token is in the Letta server's database as part of
  each agent's tool environment, so the Letta server must be trusted like the worker is.
- The registry is in memory. A turn belongs to one process, so that is enough, but two worker
  replicas behind one Service would need Letta's call to reach the replica running the turn.
  With Letta, run one worker.
- If Letta is ever worth promoting, tools that the client executes would remove the bridge
  altogether. They were not used: the self-hosted 0.16.8 server was not checked for them.
