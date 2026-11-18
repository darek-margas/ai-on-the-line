# Asking Vodafone for its best iPhone deal

Three calls, one question: the best subsidised iPhone deal. [Optus](optus-iphone.md) · [Telstra](telstra-iphone.md) · [Vodafone](vodafone-iphone.md) · [comparison](vodafone-iphone.md#three-carriers-one-phone)

The third call, the same morning as [Telstra](telstra-iphone.md). Vodafone's contact page only offers chat and a "join Vodafone" button; its support pages give 1300 650 410 for anyone not calling from a Vodafone mobile.

## Setup

The brief from the second Telstra call with Vodafone's name in it. Two changes: the must-not also rules out mentioning a detail alongside another answer (the colour slipped out that way with Telstra), and the menu hint is generic, because I didn't know Vodafone's menu. It's a new number, so it waited for approval:

![The call page waiting for approval, with the opening line, questions and what the agent may share](../images/approval-vodafone.png)

## The call

Just under six minutes, about one of them in the queue:

```
Voice bot:    Welcome to Vodafone. Please tell us in a sentence how we can help you today.
Agent:        New mobile plan with a phone.
Voice bot:    I think you want to make a purchase. Is that correct?
Agent:        Yes, that's right.
Voice bot:    Great to hear. The new iPhone Duo is now available for purchase. You can explore
              our mobile plans, devices and sign up online at vodafone.com.au. If you'd prefer
              to speak with someone, stay on the line and I'll put you through to our team.
Voice bot:    Your call may be recorded for training and business purposes. You may also receive
              a brief survey via text about your interaction with us today. We appreciate your
              feedback about your experience.
Agent (auto): This call is being recorded on our side as well.
Voice bot:    Thanks for waiting. All our advisors are currently busy. We'll answer your call.
Roslina:      Thank you for calling Vodafone. You are speaking with Roslina from Sales and
              Upgrades. May I get your Vodafone contact number?
Agent:        Hi, I'm Darek's assistant, calling on his behalf. He's thinking of moving to
              Vodafone and is after your best deal on an iPhone 18, with the phone subsidised in
              exchange for a contract. What would you offer?
Roslina:      Well, I'll have to speak with the person in order to check for the offers that we
              can discuss.
Agent:        Darek's not a Vodafone customer yet, so there's no number. Could you share your
              best new-customer iPhone 18 offers?
Roslina:      Yes, we do have iPhone 18, but nothing on the device, but we can give on the plan.
              So we have a good plans offer.
Agent:        Oh okay, good to know. Which plan would you suggest, and what's the monthly cost?
Roslina:      Yes, so we do have a plant that are available from small to medium to large. So
              does he make any international calls?
Agent:        Not that I know of. Which plan would you suggest for the phone, and what's the
              monthly cost?
Roslina:      I'll have to speak with the person in order to discuss the same. So when they are
              available, can you ask them to give us a call back?
Agent:        Understood. Just so I can pass it on, what's the monthly price on your large plan,
              even roughly?
Roslina:      That would be $78. But right now, as we are running with the Black Friday offer,
              we do have a straight offer on that, which would give a $23 off on a $78. So your
              monthly bill would come to $55, with double data.
Agent:        Oh nice, that's handy. Is that fifty-five a month with a contract, and for how
              many months?
Roslina:      a data
Agent:        Sorry, I missed that. Is the fifty-five dollars a month on a contract, and how
              long is the term?
Roslina:      The plan will not be in a contract; that will be month to month. However, it would
              be for 12 months that he would get the discount, and he would also get double
              data. Right now we do have, for the large plan, 20 gigabytes, or a double data
              that would be for 840 gigabytes.
Agent:        Got it, thanks. So no discount on the phone itself, even with a contract?
Roslina:      Currently, all the devices that we do have come directly from the manufacturers.
              So we get only discounts from the manufacturers. But yes, we could give a good
              offer on the plan.
Agent:        Right, that makes sense. So what's the iPhone 18 Pro Max 256 GB price outright, or
              monthly on a repayment plan?
              [The agent names the model and storage before being asked, against its brief.]
Roslina:      Let me check that for you, David.
Roslina:      Yes, for the iPhone 18 Pro Max 256 GB, the overall cost of the device is $2,299,
              and for 36 months that will be $63.86.
Agent:        Great, that's really helpful. So that's thirty-six months on the phone, with the
              plan itself month to month?
Roslina:      That's correct.
Agent:        Perfect. So I can get the iPhone 18 Pro Max 256 GB for sixty-three eighty-six a
              month over thirty-six months, with the large plan month to month?
Roslina:      Yes.
Agent:        Brilliant, that's everything I needed. I'll pass it all to Darek and he'll decide.
              Thanks so much, Roslina! Thanks, bye!
```

The report: no discount on the phone ($2,299, or $63.86 a month over 36 months), a large plan at $78 a month reduced to $55 for 12 months with double data, month to month with no contract. Roslina confirmed the summary.

## What went well

Vodafone's voice bot asked for a sentence. "New mobile plan with a phone" got "I think you want to make a purchase. Is that correct?", a yes, and a transfer to sales.

The recording notice went to the voice bot, before Roslina came on.

Roslina twice said she needed to speak to the customer and asked for a callback. The agent didn't argue and didn't give in: it said Darek isn't a customer yet, asked for the large plan's price "even roughly", and kept going one question at a time until it had the plan, the term, the phone price and her confirmation.

"a data" came through as her whole answer to a question. The agent said it missed that and asked again.

## What didn't

The recording notice came one sentence late. "Your call may be recorded for training and business purposes" and the survey message after it arrived as one utterance, so the reply went after both.

It named "the iPhone 18 Pro Max 256 GB" in its own question about the phone price, before Roslina had asked which model. The must-not said not to, in so many words. That's twice across these calls, so a line in the brief isn't enough here; it probably needs to be a general rule in the system prompt: a detail is given only in answer to a question about that detail.

## Three carriers, one phone

All three charge Apple's price for the phone, spread over 36 months: $63.86 a month, $2,299 in total. Nobody subsidises it. The only difference is what the plan costs, and that's where the Black Friday offers are:

| | Plan | Discount | 36 months, phone and plan |
|---|---|---|---|
| [Optus](optus-iphone.md) | $60 | $39 for the first 12 months | about $4,210 |
| [Telstra](telstra-iphone.md) | $74, 50 GB | $37 for the first 4 months | about $4,815 |
| [Vodafone](vodafone-iphone.md) | $78, 20 GB | $55 for the first 12 months | about $4,830 |

The totals are my arithmetic from the quotes. None of the three salespeople offered one.

---

Part of [AI on the Line](https://github.com/darek-margas/ai-on-the-line) by Darek Margas. This page is licensed under [CC BY-SA 4.0](../LICENSE.md).
