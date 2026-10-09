# 0035 A login link is spent by its button, not by being opened

## Context

The word `dashboard` gets an adult a one-time login link. `GET /login/{token}` spent the token
and set the session cookie. On the first household with real Telegram, the owner tapped the link
and landed on the signed-out page, whose button is "Open Telegram". The proxy's access log for 9
October 2026 shows why: the first GET of each link came from a Telegram address moments after the
message was sent, and was answered 303. The owner's own request came later and was answered 401.

A chat app opens a link it is sent, to draw a preview. So do mail scanners and some browsers that
load a page ahead of the tap. A GET cannot tell these from the person, so a GET must not be what
spends something that works once. The presence link was fixed the same way (ADR 0033).

## Decision

- **A GET of the link is a page.** It has one button, "Open my dashboard", and writes nothing. It
  looks the token up only to choose between the button and the words for a link that no longer
  works.
- **The button is a POST to the same address, and only that spends the token.** Expiry (10
  minutes), single use and the limit of 5 links an hour are unchanged, and so is the cookie.
- **The page gives a fetcher nothing.** The form has no `action`, so the token is not in the
  page; neither is anyone's name. It is served `no-store`, `noindex` and `Referrer-Policy:
  no-referrer`, as is the page for a signed-out visitor, because either can sit at an address
  with a token in it.
- **A link that was used or has expired says so**: "This link no longer works. It was already
  used or has expired. Send the word dashboard to Home for a fresh one", above the steps.
- **Telegram is asked not to preview.** Every `sendMessage` carries
  `link_preview_options: {"is_disabled": true}`. This is the second line of defence: the shop
  and calendar links hold tokens too, and no message this app sends needs a preview.

### CSRF on a POST with no session

Every other POST carries a token tied to the session. This one cannot, because the session is
what it creates. It does not need one for the usual attack: the address holds 32 random bytes
that were sent only to the person's own chat, so another site cannot name it, and cannot make
the request on the person's behalf.

What is left is the reverse: a stranger with a link of their own posts it from their site, so the
visitor is signed in to the stranger's household without knowing. The old GET allowed exactly
this with a plain link. The POST is refused with 403 when the browser's `Sec-Fetch-Site` header
says the request came from anywhere but this site. A browser too old to send the header is let
through; the stranger would have to be an adult in a household on this server, and gains a
visitor to their own pages.

The header was chosen over `Origin` because a page served `no-referrer` sends `Origin: null`
with its own form.

## Consequences

- One more tap to log in.
- A preview fetcher that runs scripts and presses buttons would still spend the link. None is
  known to; Telegram's does not.
- Whoever holds an unused link can still use it, as before. It is still a secret for 10 minutes.
- The other links sent in chat were checked. The shop link's GET is a page that only reads (ADR
  0033). The calendar link's GET returns the feed and writes nothing, and is meant to be fetched
  again and again. An invite is a `t.me` link or a code typed into the chat, and is accepted by a
  message from the person, never by a request to this server. `/setup` creates the household on
  POST.
- Nothing was changed for WhatsApp or iMessage, and neither was tried. Meta's samples send a
  text with `preview_url` set to ask for a preview, and this app never sets it. BlueBubbles'
  documented send call has no field for previews, and the Messages app draws its own. On both,
  the button is the defence.
