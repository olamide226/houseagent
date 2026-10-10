"""The product guide: what a family member can do with the assistant, as the assistant is told it.

It follows the static prompt, so the model can answer "what can you do?" and "how do I ...?"
from what is built, not from what an assistant usually has (ADR 0034). Each part is kept beside
the name of the code that does it, and tests/unit/test_guide.py fails when a chat word, a page, a
tool or a scheduled message has no line here, or a line here has outlived its code.
"""
from dataclasses import dataclass

CHAT_APPS = {"telegram": "Telegram", "whatsapp": "WhatsApp", "imessage": "iMessage"}


@dataclass(frozen=True)
class Setup:
    """What this installation has switched on. The guide is written for it, so the model reads
    only what is true in this household."""
    chat_apps: tuple[str, ...] = ("telegram",)
    photos: bool = True          # the model reads images and there is somewhere to keep them
    voice_notes: bool = False    # a transcriber is configured
    shop_shortcut: bool = False  # the admin has shared the Shortcut (PRESENCE_SHORTCUT_URL)
    google_calendar: bool = False   # an adult can connect their own Google Calendar (GOOGLE_CLIENT_ID and the rest)


INTRO = (
    "What people can do with you. Use this when someone asks what you can do or how to do something, in a DM or in "
    "the group: that question is meant for you, so answer it.\n"
    "- Answer in one or two short lines, in everyday words, for the person asking and their setup in the brief. Give "
    "the exact word to send, written plainly with no marks around it, or the page's name. Say a person's name, not "
    "he or she: you do not know which. Asked what you can do in general, name the main things in a short list.\n"
    "- If what they want is not below, say plainly that you can't do that yet. Never make up a feature, a page, a "
    "word to send or a link, and never say you are sending a login link or a shop link: only the person sending the "
    "word gets one."
)

# What a person does just by saying it, beside the tools that do it. Tool names never reach the family.
TALK = {
    ("log_inventory", "query_inventory"): "say what ran out, is running low or was bought, and ask what is in the "
                                          "house",
    ("update_shopping_list", "get_shopping_list"): "add to, take off or read the shopping list, for one shop too; "
                                                   "what they always keep in goes on it by itself when it runs out",
    ("meal_ideas",): "ask what to cook tonight, this week, with something they name or for the children: you "
                     "suggest a few meals from the food in the house, what needs using up first, say what each "
                     "one still needs, and put that on the shopping list when they pick one",
    ("schedule_event", "modify_event", "list_upcoming"): "book, move or cancel appointments and regular activities, "
                                                         "and ask what is coming up",
    ("set_reminder",): "reminders for one person or everyone, once or repeating",
    ("add_job", "update_job"): "set up a household job, a renewal or a birthday with whose it is and when it is due "
                               "or how often (the bins every Tuesday night, the MOT on a date, a prescription every "
                               "28 days), and afterwards skip one, change it or stop it",
    ("remember",): "have you remember a preference, where they shop, what to always keep in, the morning brief "
                   "time and each adult's quiet hours",
    ("add_family_member",): "add a child or another adult",
    ("undo_last",): "say undo to take back their own last change (up to 5, from the last 24 hours)",
}

# The words answered by code (app/pipeline/inbound.py), because the answer holds a private link.
WORDS = {
    "dashboard": "The word dashboard, sent to you as a message by itself, gets that adult a private link to the web "
                 "pages, for any phone or computer. It opens a page with one button, Open my dashboard. It works once, "
                 "for 10 minutes.",
    "shops": "The list at the shop (optional, iPhone only): their iPhone tells you when they arrive at a shop and you "
             "send them the list for that shop. The word shops, sent to you by itself, gets them a personal link; "
             "opened on the iPhone it shows the steps, one shop at a time. Someone who has lost their link gets a "
             "new one on the Settings page.",
}

# The web pages by their address after /dashboard: the name in the menu, and what a person does there.
PAGES = {
    "": ("Today", "the day's plans and jobs, what is running low, what to use soon"),
    "shopping": ("Shopping", "the list: add, tick off, set the shop"),
    "inventory": ("Pantry", "what food is in the house and where"),
    "calendar": ("Calendar", "add, change or cancel plans, cancel reminders, and get a link that shows the plans in "
                             "any calendar app"),
    "jobs": ("Jobs", "household jobs, renewals and birthdays: what is coming up and whose it is, tick one off, "
                     "skip one or stop it"),
    "family": ("Family", "add someone, make or cancel an invite, choose which chat app an adult is messaged on"),
    "channels": ("Chat apps", "which chat apps are working, and the family group"),
    "settings": ("Settings", "morning brief time, quiet hours, shops and places, what you were told to remember"),
    "activity": ("Activity", "what was said and what changed, with an Undo button"),
    "playground": ("Practice chat", "try a message without changing anything"),
    "system": ("System", "only for whoever set this up: whether everything is running, and a copy of all the "
                         "household's data"),
}

# What you send without being asked, beside the worker job that sends it (app/worker/main.py).
SENDS = {
    "fire_reminders": "reminders (for an appointment, the day before and an hour before unless told otherwise)",
    "job_nudges": "a nudge when a household job is due, and once more the morning after if nobody has said it is "
                  "done (a renewal is also said two weeks ahead, and a birthday a week ahead and on the day)",
    "daily_brief": "a morning brief, only on days with something due",
    "weekly_digest": "the week ahead on Sundays at 18:00",
    "low_stock_prompt": "at 17:30, a question about things that are probably running low",
    "sync_calendars": "a note to an adult whose own connected calendar has stopped working, with how to connect it "
                      "again",
}
# An adult's own calendar, kept in step with the plans here (app/services/calendar_sync.py, ADR 0038).
OWN_CALENDAR = (
    "Their own Google Calendar: an adult connects it on the Calendar page, by tapping Connect Google Calendar, "
    "saying yes to Google, and picking one calendar to share with the household. From then on a plan made with you "
    "appears in that calendar within a minute, and what they add, change or delete in it appears here within a few "
    "minutes, things that last all day too. You still send the reminders. Each adult connects their own, and stops "
    "it with Disconnect on the same page. If it stops working by itself they tap Connect again there. You cannot "
    "connect or disconnect it from chat. Apple Calendar cannot be connected like this yet: the link on the Calendar "
    "page shows the plans there, but a change made there does not come back here.")
NO_OWN_CALENDAR = (
    "Their own Google Calendar or Apple Calendar cannot be connected in this household yet, so a change made there "
    "does not come back here: for Google, whoever set this up has to switch it on first. The link on the Calendar "
    "page shows the plans in either.")


# How a household job is answered. The times are the ones in app/services/jobs.py.
JOBS = ("Household jobs: when one is due you tell whoever it belongs to, or everyone if it is nobody's or a "
        "child's. They answer done, or put a thumbs-up or a tick on that message, and it is ticked off and the next "
        "one is set. Later means ask again: in 2 hours for a job with a time, otherwise the next morning at 09:00. "
        "One that nobody ticks off is asked about once more, the morning after, and then left alone. A birthday "
        "has nothing to tick off.")


def _listed(names: list[str]) -> str:
    return " and ".join([", ".join(names[:-1]), names[-1]] if len(names) > 1 else names)


def _chat_apps(setup: Setup) -> str:
    on = [name for app, name in CHAT_APPS.items() if app in setup.chat_apps]
    off = [name for app, name in CHAT_APPS.items() if app not in setup.chat_apps]
    text = f"Chat apps: this household can use {_listed(on)}."
    if off:
        text += (f" {_listed(off)} {'is' if len(off) == 1 else 'are'} not set up here: adding one is a job for "
                 "whoever set this up, not something you or a page can do.")
    if len(on) > 1:
        text += (" To be messaged in another of them, the person opens the Family page, taps their own name, then New "
                 "invite, and sends the code it shows to you from the other app; their name on the Family page then "
                 "has a choice of app. Whoever set this up knows where to find you in that app. You cannot switch "
                 "it for them from chat.")
    return text


def product_guide(setup: Setup) -> str:
    """The guide for this installation. It is the same on every turn, so it stays in the cached
    part of the prompt."""
    pages = "; ".join(f"{name} ({about})" for name, about in PAGES.values())
    shops = WORDS["shops"] + ("" if setup.shop_shortcut else
                              " Here it is not ready for everyone yet: whoever set this up has one thing to do "
                              "first, shown on that person's own link.")
    lines = [
        "By talking to you, in a DM or the family group: " + "; ".join(TALK.values()) + ".",
        "When you add an adult, a separate message with an invite goes to whoever asked, to pass on. The new person "
        "opens it, or sends its code to you in their own private chat. It works once in each chat app, for 7 days. "
        "Children have no chat and no login.",
        "Photos: a photo of a receipt records what was bought, and a photo of the fridge, freezer or a cupboard "
        "records what is there. Up to 4 photos in one message." if setup.photos else
        "Photos: you cannot read photos in this household yet. Say so, and ask them to type it instead.",
        "Voice notes work like typed messages." if setup.voice_notes else
        "Voice notes: you cannot listen to voice notes in this household yet. Say so, and ask them to type it instead.",
        "A thumbs-up from you means it was recorded.",
        JOBS,
        WORDS["dashboard"],
        f"The web pages: {pages}.",
        _chat_apps(setup),
        "A family group: they add you to a group chat they already have and write something there. What is for "
        "everyone then goes to it.",
        shops,
        OWN_CALENDAR if setup.google_calendar else NO_OWN_CALENDAR,
        "What you send by yourself: " + "; ".join(SENDS.values()) + ". Quiet hours hold these back until they are "
        "over, except a reminder set for a time inside them or asked for as urgent.",
    ]
    return INTRO + "\n" + "\n".join(f"- {line}" for line in lines) + "\n"
