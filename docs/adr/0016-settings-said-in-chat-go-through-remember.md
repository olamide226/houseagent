# 0016 Settings said in chat go through `remember`

## Context

The onboarding steps in spec section 12.2 write things no tool in the twelve-tool contract can
write: staples (`items.is_staple`), shops (`places`), the morning brief time
(`households.digest_time`) and quiet hours (`members.quiet_start`, `quiet_end`). The spec also
says any step can be skipped and done later "just by talking", so whatever writes them must be
available after onboarding too. Until now the brief time and quiet hours could only be changed
in SQL.

## Decision

`remember` stays the one tool for durable facts and preferences, and a few keys are settings
that are written where the rest of the system reads them:

| Key | Value | Written to |
| --- | --- | --- |
| `staples` | comma-separated items | `items.is_staple`, creating items that are new. No fact row |
| `morning_brief` | a time of day | `households.digest_time`. No fact row |
| `quiet_hours` | `start-end`, or `off` | `members.quiet_start` and `quiet_end` for the member named in `about`, or every adult. No fact row |
| `shops`, `main_supermarket` | names | a fact row, and a `places` row of kind `store` per name |

Every other key is a plain row in `household_facts`. The keys are named in the tool's argument
description. Times are parsed leniently (`07:30`, `7.30`, `7am`, `9:30pm`); anything else is an
`ERROR:` that tells the model the format. The dashboard Settings page calls the same service
functions.

## Consequences

- The tool list stays at twelve and matches the spec; the model learns the keys from one
  description instead of a thirteenth tool sent with every request.
- A setting is stored once, where the router, the brief job and the stock rules already look.
  Saying "we always need rice" makes rice go on the list when it runs out, with no second lookup.
- The meaning of `remember` now depends on the key. A model that invents its own key
  (`brief_time`, say) saves an inert fact instead of changing the setting. The Settings page shows
  facts, so this is visible, and the eval suite has cases for each setting.
- Shop names are split on commas only, so "Marks and Spencer" stays one shop and "eggs and milk"
  is one staple unless the model uses a comma, as the description asks.
- A staple can be set from chat but only unset on the item's dashboard page, or by undo.
