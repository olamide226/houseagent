# 0039 A letter is one call, a day with no time is a reminder, and a doubtful date is asked about

## Context

A school letter, a party invite or an appointment message holds dates that somebody has to copy
into a calendar. People want to send the thing on and be done: a photo of the letter, the message
forwarded, the words pasted in, or the PDF the school emailed.

The assistant could already read a photo, and `schedule_event` and `set_reminder` could already
record what was in it. Three things were missing.

- **One undo.** A letter with three dates became three calls, and "undo" took back the last one.
- **A day with no time.** "Friday 23 October is an INSET day" and "return the slip by Friday" have
  a day and no time. An event needs a start time, and no all-day event can be made in chat. A model asked
  for a time it does not have invents one.
- **A date that cannot be right.** "Wednesday 12 November" in a year when the 12th is a Thursday.
  A model picks one and says nothing.

Two more were found on the way: a forwarded message reached the model as if the person had typed
it, so "tomorrow" in it counted from the wrong day; and a file sent on Telegram was dropped.

## Decision

**A sixteenth tool, `add_dates`, takes every dated thing in what was sent, in one call.** The
spec's tool list was twelve ([ADR 0016](0016-settings-said-in-chat-go-through-remember.md) kept it
at twelve on purpose); meal ideas and household jobs made it fifteen on the same day
([ADR 0036](0036-meal-ideas-from-a-short-stock-read.md),
[ADR 0037](0037-a-household-job-is-a-reminder-that-is-to-be-done.md)). This one is added because one call is
one logged action, and one logged action is what one undo reverts: the batch is the feature. Making `schedule_event` take a list
would have changed the tool every existing calendar case uses, and a day with no time is not an
event at all.

- **An entry with a time** goes through `calendar.schedule_event`, as a typed appointment does,
  with the usual reminders a day and an hour before.
- **An entry with a day and no time** becomes one reminder with the day in its words
  (`calendar.remind_of_day`): at 18:00 the evening before, or at 07:30 that morning when the
  evening has gone. It goes to the household when a child is in it, as an event's reminders do.
  An all-day event would be the better home for it. While this was being built there were none;
  two-way calendar sync then brought them in for events that come from Google
  ([ADR 0038](0038-a-connected-calendar-is-a-second-copy-kept-in-step.md)), with no reminders of
  their own. Putting a letter's days there as well is left for its own change. Until then such a
  day is in the reminders, and not in the calendar feed or a connected Google Calendar.
- **An entry is held back, and the rest is still added**, when the weekday the text gives is not
  the weekday of its date, or when the model says it is unsure of the day. Code can check a
  weekday against a date; only the model can say that "this Friday" in an undated letter is
  unclear. Both come back as an `AMBIGUOUS:` line and the model asks one question.
- **The weekday comes from the words, where there are words.** The first design trusted the
  model to pass the text's weekday on. In the first run of the whole suite it did not, for the
  one entry where it mattered, and the wrong date was added without a question. Code now finds
  the weekday beside that date in the turn's own words, and believes it over the model's. A photo
  has no words for code to read, so there the model's `weekday` is still the only check. The
  model's own sums are not believed the other way either: a doubt that only says a date "is a
  Monday", when it is the Thursday the text says, is overruled and the entry is added.
- **What was held back is in the next turn's brief.** A photo or a file is not shown to the model
  again, and a held entry writes no row, so the answer "the Thursday" would arrive with nothing to
  add. The brief reads the `AMBIGUOUS:` lines of the speaker's last turn in that thread from
  `messages.meta`, for 24 hours. It is the same reason as the "last change" line of
  [ADR 0031](0031-rules-the-model-kept-breaking.md): history holds what was said, not what the
  tools answered.
- **A date that has passed is left out and said**, not asked about.

**A forwarded message is marked as one.** Telegram says who first sent it and when
(`forward_origin`), WhatsApp only that it was forwarded (`context.forwarded`), iMessage nothing.
The line above the text reads `[forwarded message from Coach Bisi, first sent Sun 4 Oct 2026
20:00]`, in household time, and the prompt says that a relative day counts from then.

**A PDF is read as words, in the pipeline, with `pypdf`.** It is the one new dependency. The
other way is to hand the file to the model. Anthropic's and OpenAI's own APIs document a file
input; the neutral LLM types here have none, and whether DeepSeek's endpoints or the two
subscription CLIs would take one was not tried. Words need nothing from the provider: they work
the same on each, and are tested without a model. `pypdf` is pure Python with no system library,
so the image only gains a package. What it costs: a scanned PDF has no words to read. The
assistant then says so and asks for a photo of the page, which it can read.

## Consequences

- The tool list is sixteen, and every model step reads 1,208 more tokens in the cached part of
  the prompt (6,966 to 8,174 on a one-step turn, measured): the tool's description and arguments,
  one rule in the static prompt and the guide's lines. It is the dearest tool in the list, because
  each entry has eleven arguments and each needs a sentence.
- Reading a file is bounded: 15 MB, the first 10 pages, 8,000 characters, 20 seconds. Past any of
  them the assistant sees part of the file or none, and says which.
- The words of a PDF are kept in `messages.media` with the message, as a voice note's transcript
  is, and are in the thread's history for later turns. The file itself is kept and deleted like a
  photo.
- Text from a letter, a forward or a file is somebody else's writing, read by the model as part
  of a family member's message. No tool can name a recipient or reach outside the household, so
  the most it can do is put wrong entries on that household's calendar, which one undo removes.

## Not done

- An all-day event for a day with no time, so that it shows in the calendar feed and in a
  connected Google Calendar. The service can now make one; `add_dates` does not yet.
- A scanned PDF, a Word file or a spreadsheet. Only a PDF with words in it is read.
- An email sent straight to the assistant. It has no address; a person forwards or pastes.
- A photo or a file is not shown to the model a second time, so a question about the letter two
  turns later is answered from what was recorded.
