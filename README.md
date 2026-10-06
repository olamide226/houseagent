# Household Agent

A household assistant two adults talk to over chat. It keeps track of food stock, a shared
shopping list, appointments and reminders, so nobody has to remember things or fill in forms.

One FastAPI service, one worker process, one Postgres database. Channel-agnostic, model-agnostic.

**Status: milestone 3 of 6.** Telegram, inventory, the shopping list, undo, appointments,
recurring activities, reminders, a morning brief, a calendar feed, conversational setup, invites
for the rest of the family, receipt and fridge photos, and a small web dashboard work today.
WhatsApp, iMessage and presence are specified in [docs/spec.md](docs/spec.md) and not built yet.

Start with [docs/README.md](docs/README.md): a five-minute local run and links to everything else.
