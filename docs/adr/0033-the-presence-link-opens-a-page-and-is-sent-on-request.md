# 0033 The presence link opens a page, and is sent when someone asks

## Context

Setup ended by sending every adult their presence link with the phone steps in one paragraph
(ADR 0025): "Get Contents of URL with that link, method POST, and a JSON request body with two
text fields". The first person to receive it did not know what it was, tapped the link, and saw
"Method Not Allowed", because the address only answered POST. The app is for a family, not for
the engineer in it.

Apple's Shortcuts User Guide (read 7 October 2026) says a shortcut can be shared as an iCloud
link, with *import questions* that ask each person for their own value. It also says automations
are specific to a device, and describes no way to share one or to pass anything from an
automation to its shortcut. So the Arrive step cannot be installed for anyone: each person makes
it by hand, once per shop.

## Decision

- **A GET of the link is a page.** `GET /presence/{token}` says in two sentences what the link
  does, that it is optional, and the steps, in the dashboard's style and in the words the
  Shortcuts app shows. It reads the household's shops and writes nothing: a ping is always a
  POST, so a chat app fetching a preview, or a tap, records nothing.
- **A copied link is a whole ping.** `POST /presence/{token}/{event}/{place}` is the ping with
  nothing in its body. The page offers one such link per shop, with a Copy button. A shortcut then
  holds one pasted link, and nobody types a place name or builds a JSON body. `POST
  /presence/{token}` with a JSON body is unchanged.
- **One shared Shortcut, set as `PRESENCE_SHORTCUT_URL`.** The admin builds the one-action
  shortcut once, gives its link an import question and shares it. Every page then has a "Get the
  shortcut" button. Until it is set, the admin's page shows how to make it and everyone else's
  says to ask them: the words POST and URL are shown to the one person who needs them.
- **Setup sends an offer, not a link.** The seventh step sends each connected adult one short
  message: what it is, that it is optional, and "send me the word shops". An adult who connects
  later gets the same after the welcome. It is sent once per person (an outbox dedupe key).
- **The word `shops` gets the link**, answered by the pipeline like `dashboard`, so the token
  never passes through the model. It makes a first link only. Someone who has one is told where to
  find it and that Settings can replace it: a new token would stop the shortcuts on their phone.
- **The page is private.** It is served `no-store`, `noindex` and `Referrer-Policy: no-referrer`,
  because its address holds the token and it links to icloud.com.
- **`/static` is on the public ingress** whatever `dashboard.public` says, since the page needs
  the stylesheet on a phone outside the house (this amends ADR 0030).

## Consequences

- A GET tells a valid token from an unknown one (200 and the shops, or 404). ADR 0023 kept POST
  from doing so. The token is 32 random bytes and cannot be guessed, and whoever holds a valid
  one could already send pings; what the page adds for them is the names of the family's shops.
- There is still no one-tap setup. With the shared shortcut it is copy, add, and one automation
  per shop; without it nobody but the admin can finish.
- None of the phone steps has been tried on a phone. [presence.md](../presence.md#what-apple-allows)
  lists what Apple's guide says and what it leaves unsaid.
- A token is no longer made for someone who never asked for one.
- A place name with a `/` in it still works in a copied link: the rest of the path is the name.
- A household set up before this change keeps its links. They open the page now.
