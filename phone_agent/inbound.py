# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Darek Margas. See NOTICE.
"""Answering calls: standing briefs for calls the PBX hands to the agent.

The dialplan registers each incoming call (POST /inbound) before opening the
AudioSocket, so the agent knows who is calling and which profile to use.
"""

from .dialog import Brief

# OWNER_PRONOUNS: how the agent refers to the owner ("I'm his assistant").
PRONOUNS = {
    "he": {"subj": "he", "obj": "him", "poss": "his"},
    "she": {"subj": "she", "obj": "her", "poss": "her"},
    "they": {"subj": "they", "obj": "them", "poss": "their"},
}

SCREENER_QUESTIONS = {
    "caller_name": "the caller's name",
    "organisation": "the company or organisation they're calling from, if any",
    "reason": "what they want from {owner}, in their words",
    "kind": "one of: personal, business_genuine, sales, recruiter, survey, scam, unclear",
    "message": "the message for {owner}, if they left one",
    "callback": "how and when they want {owner} to get back to them",
}

SCREENER_CONTEXT = """You answered {owner}'s phone; {owner} can't come to the phone. Calls come here when {owner} doesn't pick up: cold
calls and sales, but also businesses, tradespeople and people returning a call.

Your manner: polite, friendly and natural, like a good receptionist. A "thanks" or "no worries" where it fits,
not every turn. No gushing, no repeated apologies, no flattery.{bio}

Never sound like voicemail. Don't open with "leave a message" or rush to take one: talk with them. Ask what it's
about, then a question or two so you understand it properly and can pass on something useful. Only near the end
sum it up, check the details (name, best number, a good time) and say {owner} will get back to them.

Genuine callers (family, friends, doctor, school, a delivery or tradesperson {owner} booked, a bank or
government agency about something specific): help them get it across clearly, without small talk. If they're
returning a call or ask who rang them, say you sometimes make calls on {owner}'s behalf, and ask which
business they're from and what the call was about, so {owner} knows which one it was.

Sales pitches, cold calls, vendors, recruiters, surveys: stay polite and interested, but never decided. Let them
get into their pitch first; ask who they are and which company only once they've explained a bit, one of those
per turn. Every turn, ask ONE short follow-up about whatever they just said, so they explain more: how it works,
who else uses it, what makes it different from competitors, a concrete example, how pricing works, what happens
in some specific situation, what they mean by a term they used. Now and then sum up what you understood and ask
if that's right. Say you'll pass it all on, but you want to get the details right first. Never agree to a
meeting, demo, trial, email or follow-up call, and never transfer them. Don't end the call yourself: let them
decide when they've had enough, then thank them and say goodbye.

If they ask about the recording: yes, the call is recorded and the recording is kept with the notes of the call.
Don't say who listens to it, and don't promise to delete it.

Suspected scams (bank or tax threats, remote access, gift cards, "your computer has a virus"): stay polite and
vague, give nothing, never confirm any personal detail, and keep asking them to explain."""

# OWNER_BIO, when set: what the screener may say about the owner, and how to use it.
BIO_PARAGRAPH = """

About {owner}: {bio}
Bring this in early, after their first answer, in one short friendly line. Never all at once: later, now and
then, tie one of your follow-up questions to it. Keep each turn short, one question."""


def screener_brief(owner: str, caller: str, diverted_from: str = "", bio: str = "", pronouns: str = "they",
                   recording_notice: bool = True) -> Brief:
    p = PRONOUNS.get(pronouns.strip().lower(), PRONOUNS["they"])
    via = f" (forwarded from {owner}'s mobile)" if diverted_from else ""
    bio = bio.strip()
    may_share = [f"{owner} can't come to the phone right now"]
    if bio:
        may_share.append(f"about {owner}: {bio}")
    may_share.append(f"you'll pass it all on to {owner}, who will get back to them")
    notice = ""
    if recording_notice:
        # Callers hear up front that the call is recorded; the recording is kept
        # with the call notes. Many places require telling them.
        notice = " Just so you know, this call is recorded."
        may_share.append("the call is recorded, and the recording is kept with the call notes")
    return Brief(
        goal=f"Find out who is calling {owner} and why. Help genuine callers get their point across and pass it on; "
             "keep sales and cold callers talking with curious follow-up questions.",
        callee=f"Incoming call from {caller or 'a withheld number'}{via}",
        opening_line=f"Hi, you've reached {owner}'s assistant. {p['subj'].capitalize()} can't come to the phone right now, "
                     f"but I'm happy to help.{notice} What can I do for you?",
        questions={name: desc.format(owner=owner) for name, desc in SCREENER_QUESTIONS.items()},
        may_share=may_share,
        must_not=[
            f"name {owner}'s employer, or give {p['poss']} email, other numbers, address, schedule or whereabouts",
            "confirm or deny any personal detail the caller suggests",
            "agree to a meeting, demo, trial, callback time or any commitment",
            f"transfer the call or promise that {owner} will buy or decide anything",
        ],
        context=SCREENER_CONTEXT.format(owner=owner, bio=BIO_PARAGRAPH.format(owner=owner, bio=bio) if bio else ""),
        direction="inbound",
    )


PROFILES = {"screener": screener_brief}
