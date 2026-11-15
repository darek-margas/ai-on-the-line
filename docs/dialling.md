# Which numbers it dials

The agent checks every number before it queues a call, whatever the PBX's outbound routes would allow. The rules are settings in `.env`, and numbers are checked against Google's libphonenumber data, so they work for any country.

Out of the box it's set up for Australia: mobiles, landlines with an area code, free-call numbers and internal extensions. It refuses 13/1300 numbers, premium numbers, international numbers and anything it can't recognise.

Emergency numbers are refused in every configuration. That covers the emergency numbers libphonenumber knows for your country plus 000, 106, 110, 111, 112, 119, 911, 999 and 0118, checked before any other rule, including your own patterns. An AI calling an emergency line by mistake costs someone else time.

| Setting | Default | What it does |
| --- | --- | --- |
| `DIAL_COUNTRY` | `AU` | Your country, as a two-letter code. Numbers without a `+` are read as numbers in this country. |
| `DIAL_ALLOW` | `fixed_line,mobile,toll_free` | Number types to allow. Also available: `shared_cost` (Australian 13/1300), `premium`, `voip`, `personal`, `uan`. |
| `DIAL_INTERNATIONAL` | `false` | Allow numbers in other countries (still limited to the types above). |
| `DIAL_LOCAL_AREA_CODE` | empty | Accept numbers without an area code, e.g. `02` for Sydney. They're checked with it and dialled as given. |
| `DIAL_EXTENSIONS` | `[1-9]\d{1,4}` | Internal extensions (regex), dialled as given. Empty to allow none. |
| `DIAL_ALLOW_PATTERN` | empty | Extra numbers to allow (regex on the digits), e.g. a short code your PBX handles. |
| `DIAL_BLOCK_PATTERN` | empty | Numbers to refuse even if the rules above allow them. |
| `DIAL_FORMAT` | `national` | What the PBX gets: `national` (`0491570156`, international as `0011…` from Australia) or `e164` (`+61491570156`). |

Patterns match the whole number, digits only (a leading `+` is dropped before matching).

Some examples:

```sh
# Sydney, including 8-digit local numbers
DIAL_LOCAL_AREA_CODE=02

# UK, mobiles and landlines only, trunk wants +44...
DIAL_COUNTRY=GB
DIAL_ALLOW=mobile,fixed_line
DIAL_FORMAT=e164

# Also allow Australian 13/1300 numbers, but never one particular number
DIAL_ALLOW=fixed_line,mobile,toll_free,shared_cost
DIAL_BLOCK_PATTERN=1300123456
```

When a number is refused, `place_call` fails straight away with the reason, so Claude can tell you instead of queueing a call that would fail later.

Calling hours (`CALL_HOURS`) apply to outside numbers only; extensions can be called at any time.

---

Part of [AI on the Line](https://github.com/darek-margas/ai-on-the-line) by Darek Margas. This page is licensed under [CC BY-SA 4.0](LICENSE.md).
