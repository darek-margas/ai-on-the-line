# Paddle steamers in December

I was planning a trip along the Murray around December and wanted a paddle steamer cruise. The operator's website listed its boats and the usual cruise times, but not what runs over Christmas. This was one of the first real calls where I actually needed the answer.

The request to Claude was one line: find the paddle steamer operator in that town and ask about December, the Christmas break, and which boats will be running.

The whole call took 88 seconds. Boat names are replaced with letters below.

```
Callee: Hello, [operator], James.
Agent:  Hi, I'm calling as Darek's assistant, with a quick question about your cruises in December.
Agent:  So, do your cruises run right through December, and on which days?
Callee: Usually Saturday, Sunday, Tuesday, Wednesday, Thursday.
Agent:  Great, thanks! And do those run at the usual morning and afternoon times?
Callee: But they could change.
Agent:  Right, good to know. Do you close over Christmas and New Year?
Callee: Christmas Day and Boxing Day, we are closed. And New Year's Day, we are closed. We are open in between.
Agent:  Perfect, that's really helpful. And which boats will be running in December?
Callee: [Boat A].
Agent:  Just the [Boat A], got it. Is the [Boat B] or [Boat C] out of service then?
Callee: The [Boat B] is sold and the [Boat C] is in survey.
Agent:  Oh, I see, thanks! Last one: should Darek book ahead for December, and is that online or by phone?
Callee: Online.
```

Whisper mangled the greeting into something that sounded like a doctor's name, and turned the boat into a word that isn't a boat. Neither mattered. The agent knew from the brief which business it had called, and Claude had read the website before writing the brief, so the brief already named all three boats. The mangled name could only be one of them.

The useful part is boats B and C. The brief asked which boats would run and whether any were out of service. When the answer was one boat, the agent asked about the other two by name and got something no web page had: one boat sold, one in survey.

Claude's report said what was known and what wasn't: days of operation (subject to change), closed 25, 26 December and 1 January, only one boat running, book online. Departure times stayed unknown, because the answer to that question was "they could change".
