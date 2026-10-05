# 0005 Server-rendered HTMX dashboard instead of a SPA

## Context

The dashboard is for setup, oversight and fixing mistakes, used occasionally from a phone. A
single-page app would add a build step, a second deployable and a separate API surface.

## Decision

Render the dashboard in the api process with Jinja2 templates and HTMX partials. One hand-written
CSS file, system fonts, dark mode via `prefers-color-scheme`, no client-side state. Pages call
`app/services/` directly.

## Consequences

- No frontend build and nothing extra to deploy.
- Dashboard writes share the chat path's stock rules, undo log and outbox for free.
- If the dashboard outgrows this, a separate frontend can replace it, because pages only call the
  same internal services.
