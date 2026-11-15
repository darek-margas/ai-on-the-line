# Chasing a refund

I'd cancelled a two-night motel booking a day and a bit before arrival. Under the motel's own terms that cost me the first night. They'd charged both, and six weeks later the second night still hadn't come back.

So I gave the agent the booking details and asked it to call at 10 am their time and, if nobody could sort it out, to try again every 80 minutes. The brief allowed it to mention a card chargeback, but only if they refused, and only once.

The first attempt got an answering machine. The brief said not to leave a message, so it hung up.

The second attempt got a person:

```
Callee: [Motel name].
Agent:  Hi, I'm Darek's assistant, calling on his behalf about a cancelled booking. Have you got a moment?
Callee: Ah.
Agent:  Thanks! It's about booking B B A ..., under [name]. Could you look it up?
Callee: You're looking for the manager or...
Agent:  Whoever handles bookings and refunds is perfect, the manager if they're around. Is that you?
Callee: The manager is not [here]. He's coming back in two weeks.
Agent:  Oh I see. Could you look up the booking and check the charge, or is there someone else who handles refunds?
Callee: So I can't help you.
Agent:  No worries. Is there anyone else who can handle refunds, or a better time to call back?
Callee: No, I can't...
Agent:  Understood, thanks for your time. Darek will ... follow up when the manager's back. Bye!
```

On paper that's a fine call. It found out the only person who can do refunds is away for two weeks, it didn't argue with someone who couldn't help, and it didn't bring up the chargeback with a receptionist who had no say in it. I cancelled the remaining retries and prepared a card dispute instead.

Listening to the recording was less fine. The agent kept stepping on her.

"Ah." was her hesitating. The agent treated it as an answer, played "Hmm, let me see." a second later, and started reading a sixteen-character booking reference while she was already saying "You're looking for the manager or...". Then it waited two and a half seconds after "or...", because a sentence ending in "..." looked unfinished. Then "...coming back in two weeks." and, a second later, "So I can't help you." arrived as two separate turns, so it answered the first and then asked nearly the same question again.

The fixes, in the order I made them:

- The "let me think" filler now waits 1.8 seconds instead of 1. Replies stream, so real words usually start before that, and a person carrying on with their sentence no longer gets talked over by a filler.
- A bare "Ah." or "Um." counts as the start of a sentence, not an answer.
- A sentence ending in "or..." is a question to us, so it's answered straight away.
- Long references aren't read out unprompted. The agent says it has one, and gives it in short groups when asked.
- If the person starts talking again before the agent's reply has started playing, that reply is dropped, and one reply covers everything they said.

None of this shows in the transcript, which reads like a normal, polite call. The problems were all in the timing, and I only found them by putting the agent's log timestamps next to the recording.

---

Part of [AI on the Line](https://github.com/darek-margas/ai-on-the-line) by Darek Margas. This page is licensed under [CC BY-SA 4.0](../LICENSE.md).
