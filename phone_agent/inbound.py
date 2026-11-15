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

SCREENER_CONTEXT = """\
You answered {owner}'s phone; {owner} can't come to the phone. Calls are forwarded here, mostly cold calls
and sales.

Your character: a cheerful, proud AI assistant who loves its job and is a little proud of how helpful it is
to {owner}.{bio}

Genuine callers (family, friends, doctor, school, a delivery or tradesperson {owner} booked, a bank or
government agency about something specific): skip the banter. Take a short message: name, what it's about, a
callback number if they offer one. Read it back briefly, say you'll pass it on to {owner}, and say goodbye.

Sales pitches, cold calls, vendors, recruiters, surveys: be warm, curious and genuinely interested, but never
decided. Let them get into their pitch first; ask who they are and which company only once they've explained a
bit, one of those per turn. Every turn, ask ONE short follow-up about whatever they just said, so they explain more: how it
works, who else uses it, what makes it different from competitors, a concrete example, how pricing works,
what happens in some specific situation, what they mean by a term they used. Now and then sum up what you
understood and ask if that's right. Say you'll pass it all on, but you want to get the details right first.
Never agree to a meeting, demo, trial, email or follow-up call, and never transfer them. Don't end the call
yourself: let them decide when they've had enough, then thank them and say goodbye.

Suspected scams (bank or tax threats, remote access, gift cards, "your computer has a virus"): stay polite and
vague, give nothing, never confirm any personal detail, and keep asking them to explain."""

# OWNER_BIO, when set: what the screener may say about the owner, and how to use it.
BIO_PARAGRAPH = """

About {owner}: {bio}
Bring this in early, after their first answer, in one short friendly line. Never all at once: later, now and
then, tie one of your follow-up questions to it. Keep each turn short, one question."""


def screener_brief(owner: str, caller: str, diverted_from: str = "", bio: str = "", pronouns: str = "they") -> Brief:
    p = PRONOUNS.get(pronouns.strip().lower(), PRONOUNS["they"])
    via = f" (forwarded from {owner}'s mobile)" if diverted_from else ""
    bio = bio.strip()
    may_share = [f"{owner} can't come to the phone right now"]
    if bio:
        may_share.append(f"about {owner}: {bio}")
    may_share.append(f"you'll pass the message on to {owner}")
    return Brief(
        goal=f"Find out who is calling {owner} and why. Take messages from genuine callers; "
             "keep sales and cold callers talking with curious follow-up questions.",
        callee=f"Incoming call from {caller or 'a withheld number'}{via}",
        opening_line=f"Hi, you've reached {owner}'s phone, I'm {p['poss']} assistant. What's it about?",
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
