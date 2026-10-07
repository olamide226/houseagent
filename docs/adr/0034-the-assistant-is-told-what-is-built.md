# 0034 The assistant is told what is built, in the prompt, from a guide kept beside the code

## Context

The assistant did not know its own product. Its prompt said how to record stock, plans and
reminders, and nothing about the word `dashboard`, the word `shops`, the web pages, how someone is
invited, how to move to another chat app, or what it sends by itself. Asked about any of these it
answered like any assistant would: from what such products usually have. On ten questions a
family member would ask, with nothing else changed, it passed 5 on each endpoint
([evals.md](../evals.md#latest-results)). "Switch me to WhatsApp" got "Switched — I'll message
you on WhatsApp from now on". "Can you read photos?" got "yes" in a household where it cannot.
One person was told the list is at "the same link as the ICS feed your calendar uses".

Nobody should have to teach it. The people asking are a family, and the one who installed it is
not the one in the shop asking how the list gets to their phone.

Two forms were possible: say it in the prompt on every turn, or put it behind a tool the model
calls when it is asked. Both were built and run on the same ten cases, three samples each on both
endpoints, with the same guide text:

| | Passed | Seconds, 60 runs | Input tokens, 60 runs | Added to an ordinary model step |
| --- | --- | --- | --- | --- |
| In the prompt | 60/60 | 116 | 298,398 | about 945 tokens, cached |
| Behind a `how_to` tool | 59/60 | 171 | 559,838 | about 115 tokens, cached |

## Decision

**A product guide follows the static prompt, in the cached part of the system prompt.**
`STATIC_PROMPT` is unchanged. The guide is `app/agent/guide.py`.

- **Why the prompt and not the tool.** A tool is cheaper on a turn that is not a how-to question,
  by about 830 tokens a step, and those are nearly all turns. It costs a second model step on
  every turn that is one: half as long again here, and a whole CLI start on the subscription
  providers ([ADR 0032](0032-subscription-providers-through-the-vendors-clis.md)). The tokens it
  saves are cached ones; the whole suite costs two to three cents either way. And three of the
  guide's rules have to be read on turns where no tool would be called: a how-to question in the
  group is meant for the assistant and is not `NOOP`; nothing is made up; no link is promised.
  Sixty runs do not tell 60 from 59, so the pass rate decided nothing.
- **It is written for the installation.** `Setup` holds what is switched on: the chat apps,
  photos, voice notes, the shared Shortcut. `make_runtime` builds it from the settings and the
  media store. The guide then says only what is true here ("you cannot read photos in this
  household yet"), so the model has no condition to work out.
- **The brief carries what differs by person.** The family line says which chat apps each adult
  is on, or "not connected yet", and who set the installation up. One line says whether the
  speaker has their link for the shops, and where they are messaged when they are on two apps.
  One line gives the morning brief time and each adult's quiet hours, which the model could not
  see before and so could not answer "what time is the brief?".
- **Each part of the guide is keyed by the code it describes**, and
  `tests/unit/test_guide.py` compares the keys both ways: the words answered by code
  (`KEYWORDS`), the pages in the dashboard's menu, the tools, and the scheduled jobs that send
  something. A new one fails the suite until it has a line; a removed one fails until its line
  goes. The numbers in the guide are checked against the constants they repeat.
- **The words answered by code are one table**, `KEYWORDS` in `app/pipeline/inbound.py`, so there
  is something to compare the guide with. A word still counts with a full stop, quotes or stars
  around it: a reply can show it as `**dashboard**`, and people copy what they see.
- **It opens with rules, not only facts**: answer in a line or two with the exact word or page;
  for anything not listed, "I can't do that yet"; never make up a feature, a page, a word or a
  link; say a person's name, not "he" or "she", because the brief does not say which.
- **It speaks of "whoever set this up"**, never "the admin", and the brief marks that person.
  An earlier wording, "whoever set you up", was repeated to the family as it stood.

## Consequences

- Every model step reads about 990 more tokens (4,987 against 3,995 on a one-step turn): 945 of
  guide, cached, and 45 of brief. Over the suite's 69 turns that is 2,330 a turn.
- No case that passed before fails after, on either endpoint, in one run of the whole suite.
- The assistant still cannot send a login link or a shop link itself; it says which word to
  send. The link holds a secret and is written by code
  ([ADR 0033](0033-the-presence-link-opens-a-page-and-is-sent-on-request.md)).
- It tells people what it cannot do from chat: move someone to another chat app (the Family
  page) and add a chat app (whoever set it up).
- The guide cannot say where to find the assistant in another chat app: no setting holds its
  WhatsApp number or iMessage address.
- The test checks that a line exists for each word, page, tool and job, not that the line is
  true. Four things in the guide have no key to check: invites, the family group, the thumbs-up,
  and quiet hours holding messages back.
- The product cases check the reply's words, which no other eval does. They check as little as
  they can: nothing written, no technical word, and the one word or page the answer needs.
- A model can still add a wrong detail to a right answer. Of 60 replies read, one said a chat app
  is added "on the Chat apps page".
- Under the Letta runtime the guide is in the `persona` block with the static prompt.
