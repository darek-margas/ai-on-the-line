# Asking Optus for its best iPhone deal

Three calls, one question: the best subsidised iPhone deal. [Optus](optus-iphone.md) · [Telstra](telstra-iphone.md) · [Vodafone](vodafone-iphone.md) · [comparison](vodafone-iphone.md#three-carriers-one-phone)

My phone is due for replacement, and the honest way to compare carriers is to ask each one the same question. Sales lines are a hard test for the agent: a voice bot, a queue, and a salesperson who wants your details before giving a price. So on 17 November I scheduled a call to Optus for 9:15 and let the agent do it.

## Setup

What Claude passed to `place_call`. The model was Claude Sonnet; the call needed my approval on its page before it dialled.

```yaml
phone: 1800 508 000          # Optus sales, new customers
goal: Find Optus's cheapest firm deal for a subsidised iPhone 18 Pro Max 256 GB, black or
      similar, on a lock-in contract: the exact phone price and the plan and term it needs.
opening_line: Hi, I'm Darek's assistant, calling on his behalf. He's thinking of moving to
      Optus and wants to know your best deal on a new iPhone, with the phone subsidised in
      exchange for a contract. What would you offer?
questions:
  best_deal: the deal they bring as the best option (plan, monthly cost, term, phone cost)
  cheapest_total: lowest total over the contract for the 18 Pro Max 256 GB, and how
  discounts: bonus credit, trade-in or bring-your-number discounts, and conditions
  confirmed_summary: their yes or correction to "So I can get ... for X if I sign up for Y?"
may_share: Darek's first name; he's comparing carriers; the model only when asked;
      he's fine with a lock-in contract
must_not: lead with the model; give a surname, address, date of birth, account details,
      phone number or email; agree to a credit check or callback; sign up or accept an offer
run_at: 2026-11-17 09:15
```

## The call

Nine and a half minutes. Transcript from speech recognition, hold condensed:

```
Recording: Hi, welcome to Optus Sales.
Agent:     Hi, I'm Darek's assistant, calling on his behalf. He's thinking of moving to Optus
           and wants your best deal on a new iPhone, with the phone subsidised on a contract.
           What would you offer?
Recording: We are recording this call and using your phone number and information shared during
           the call to address your inquiry, enhance your experience, for training, verification
           and quality assurance purposes and in accordance with our privacy policy at
           optus.com.au forward slash privacy.
Recording: In a sentence, please tell me how we can help you today.
Agent:     I'm after your best subsidised iPhone deal on a contract, for a new customer.
Recording: Thanks. Connecting you to an expert. Our team is here to help. Please be respectful
           towards our staff as they work to assist you.
           [Hold music, about 2 minutes. Speech recognition hears it as “Thank you.” six times;
            the agent stays quiet.]
Recording: There's a delay in connection.
Kay:       Hi, thank you for calling. This is Kay, your service consultant. How can I help?
Agent:     Hi Kay! I'm calling for Darek, who's thinking of switching to Optus. What's your best
           subsidised iPhone deal on a contract?
Kay:       Let me check on that. But if the customer specifically got a device on their mind…
Agent:     Yes, he's after the iPhone 18 Pro Max, 256 gig, in black.
Kay:       Add it, I'm just checking it. So as of the moment, we do have a deal for iPhone 17
           where it's just like you can save at least $500, but I'm looking through the iPhone
           18.
Agent:     No worries, take your time. I'm just after the iPhone 18 Pro Max if it's available.
Kay:       Um, yep. This one is in stock with 12 to 36 months of installment with its four
           variety of colors, the black, silver, glazier, and burgundy.
Agent:     Great, black sounds perfect. What's the best plan and monthly cost with the phone, on
           the thirty-six month term?
Kay:       So we do have a Black Friday deal for several plans here, including the $60 plan,
           which is the lowest that we have, the $70 plan, and then the $90 plan. So they could
           definitely work for the iPhone 18. Whether it's six months, 12 months, or 24 months.
Agent:     Nice. On the sixty dollar plan with a thirty-six month term, what's the monthly phone
           cost?
Kay:       So the monthly cost will be depending on the storage size as well. So for the lowest,
           which is the 256GB for 36 months, this is $63.86 for the device itself.
Agent:     Got it, sixty-three eighty-six a month for the phone. Is the sixty dollar plan
           separate on top of that?
Kay:       Yes.
Agent:     Got it. So that's sixty plus sixty-three eighty-six monthly. Any bonus credit or
           trade-in discount that lowers it?
Kay:       Bye.
           [Misheard: the line breaks up for a moment.]
Agent:     Sorry Kay, you cut out a bit. Is there any trade-in or bonus credit that lowers the
           price?
Kay:       So for new services, we do have the exclusive offer that you can make a payment of
           $39 per month for a year. And then after that 12-month period, it will go back to
           $60.
Agent:     Oh nice, thirty-nine a month for the first year. Is that on the sixty dollar plan,
           and any trade-in or bonus credit on top?
Kay:       Oh, um… they also have like 120 gigabytes like a month… uh for the for the first one
           year as well, so you'll have um additional gigabytes of data. So the offer ends on
           the 6th of December, 2026.
Agent:     Oh nice, extra data too. Is there any trade-in or bring-your-number discount on top
           of that?
Kay:       Let me go ahead and have a look in there.
Agent:     No rush, Kay. Take your time checking on the trade-in.
Agent:     Hi Kay, are you still there? No rush at all.
Kay:       Anna, do we have?
           [Kay asks a colleague.]
Agent:     Hi Kay, still with me? No rush. Is there any trade-in or bring-your-number discount?
Kay:       Yep, we do have that one, which was a Black Friday deal.
Agent:     Lovely. Is that Black Friday deal the same thirty-nine a month offer, or is there a
           trade-in credit as well?
Kay:       It's the same one that I have advised you earlier. You can definitely have a check on
           this one on the Optus website as well.
Agent:     Perfect, thanks Kay. So I can get the iPhone 18 Pro Max 256 GB in black for sixty-
           three eighty-six a month over thirty-six months, if I sign up for the sixty dollar
           plan?
Kay:       Yes.
Agent:     Brilliant, that's really helpful Kay. Darek's just comparing prices today and will
           decide himself, so thanks so much for your time. Have a great day!
```

The report that came back: the iPhone 18 Pro Max 256 GB at $63.86 a month over 36 months, on the $60 plan, with new services paying $39 a month for the first 12 months. Kay confirmed the closing summary word for word. Nobody offered a total, so `cheapest_total` stayed empty.

## What went well

The voice bot asked for a sentence and got one: "I'm after your best subsidised iPhone deal on a contract, for a new customer." It routed straight to sales.

Two minutes of hold music came through speech recognition as "Thank you." six times. The agent didn't answer any of them.

Kay was hard to follow, and the line made it worse. Speech recognition turned one fragment into "Bye." and another into "Anna, do we have?" I listened to both stretches several times and still can't say what she said. The agent didn't take "Bye." as a goodbye: it said she'd cut out and asked again. When she went quiet to check something, it waited and nudged gently instead of repeating the question.

It got the one thing that matters, a firm monthly price with the plan it depends on, and had it confirmed.

## What didn't

The first line was wasted. "Hi, welcome to Optus Sales." is a recording, and the agent introduced itself to it. A recorded greeting now gets a five-second pause to see whether more recording follows; that went into release 2026.11.2.4.

The line itself was poor, well below what G.711 carries. Words were cut off into dead silence, not even background hiss, which points at heavy compression somewhere between the call centre and the network. The agent coped better than I did, but this is where the misheard "Bye." came from.

"Hi Kay, are you still there? No rush at all." followed by "Hi Kay, still with me? No rush." is one nudge too many.

---

Part of [AI on the Line](https://github.com/darek-margas/ai-on-the-line) by Darek Margas. This page is licensed under [CC BY-SA 4.0](../LICENSE.md).
