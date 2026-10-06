# 0024 Presence rules: one atomic claim per nudge, and what "the list for this shop" means

## Context

Spec section 11 has three rules: store arrival, out and about, both home. The acceptance item is
"a store-arrival Shortcut call produces the filtered list within 30 s, at most once per 2 h per
store". Phones fire automations twice, and two api replicas can receive the same ping.

## Decision

- **Every nudge is claimed in `nudge_log` first, in one statement**: an insert that, on conflict,
  updates `sent_at` only if the last one is old enough, and returns a row only when it inserted
  or updated. Of two calls at the same moment exactly one gets a row. Keys: `store:{member}:{place}`
  (again after 2 hours), `out:{member}:{local date}` (once), `low_stock:{item}` (again after 3 days).
- **Nothing to buy here, nothing sent, and no claim.** The spec's condition is "the active list
  has 1+ non-predicted items". It is applied to the list *for this shop*: a list that only has
  things for other shops sends nothing. Because the claim comes after that check, an item added
  five minutes later still gets its list on the next arrival.
- **"For this shop"** is every entry naming no shop, plus those whose `store_hint` and the place
  name have a trigram word similarity of 0.6 or more in either direction. Measured on real pairs:
  "Tesco" and "Tesco Extra" 1.0, "African shop" and "the African shop on Rye Lane" 1.0, "african
  store" and that place 0.64, "Tesco Express" and "Tesco Extra" 0.57, "Costco" and "Tesco Extra"
  0.14. The same match now serves `get_shopping_list(store=...)`, which was an exact comparison.
- **The arrival list is urgent** and goes out in quiet hours, as the spec says. **The "out and
  about" offer ignores quiet hours** too: someone who has just walked out of the door is awake,
  and held until morning it would be wrong by the time it arrived.
- **"A long list" counts real entries**, not guesses, and the message gives that count.
- **Both home**: an adult is out if the last thing their phone said about a home place is that
  they left it. An adult whose phone has never reported counts as home, or a household where only
  one person set up Shortcuts could never be "all home". When everyone is home and a shop list
  was sent today, the day's `out:` key is claimed for every adult.
- **The clock is passed in** (ADR 0013): the route reads it once and hands it to the rules.

## Consequences

- At most one list per person per shop per two hours, whatever the phone does. The two hours run
  from the last list sent, not from the last ping.
- Two adults in the same shop each get the list.
- A hint that is merely similar to another shop's name can show at the wrong shop. Showing an
  extra item is the cheaper mistake; hiding the yam at the African shop is the costly one.
- The send still waits for the outbox poll (2 s), so "within 30 s" has a wide margin: measured at
  0.3 to 1.9 s with the real worker.
