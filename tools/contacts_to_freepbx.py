"""Turn an exported vCard file (e.g. iPhone: Contacts > Lists > long-press a list >
Export) into contacts for FreePBX's Contact Manager, which the PBX checks before
screening a forwarded call.

Prints JSON: one entry per contact with displayname, fname, lname, company, title and
numbers (national form, as Internode presents caller ID: 0491570156, 0255501234;
type cell / work / home / other; faxes left out). An optional second file lists
blocked contacts, one name or number per line (as iPhone shows them under Settings >
Apps > Phone > Blocked Contacts): they're left out, so their calls still go to the
screener. Load into a Contact Manager group (replacing what's in it) with:

    python tools/contacts_to_freepbx.py contacts.vcf [blocked.txt] | \\
      ssh root@pbx php /root/load_contacts.php imported

(load_contacts.php is in asterisk/). The vCard holds other people's numbers: keep it
out of the repo and delete it after.
"""

import json
import re
import sys


def unfold(text: str) -> list[str]:
    """vCard lines continue on the next line when it starts with a space or tab."""
    lines: list[str] = []
    for raw in text.splitlines():
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def national(number: str) -> str | None:
    """'+61 491 570 156' -> '0491570156'; '5550 1234' -> '0255501234' (Sydney).
    None for numbers that can't arrive as an Australian caller ID."""
    digits = re.sub(r"[^\d+]", "", number)
    if digits.startswith("+61"):
        digits = "0" + digits[3:]
    elif digits.startswith("0061"):
        digits = "0" + digits[4:]
    elif digits.startswith("001161"):  # saved with the 0011 international prefix
        digits = "0" + digits[6:]
    elif digits.startswith("61") and len(digits) == 11:
        digits = "0" + digits[2:]
    if digits.startswith("0") and digits[1:2] in ("2", "3", "4", "7", "8") and len(digits) == 10:
        return digits
    if re.fullmatch(r"[2-9]\d{7}", digits):
        return "02" + digits  # local number saved without an area code: assume Sydney
    if re.fullmatch(r"1[38]00\d{6}|13\d{4}", digits):
        return digits
    return None


def unescape(value: str) -> str:
    """vCard escapes undone, trimmed to what Contact Manager stores: 100 characters,
    no emoji (its columns are utf8mb3)."""
    value = value.replace("\\,", ",").replace("\\;", ";").replace("\\n", " ").replace("\\\\", "\\")
    return "".join(ch for ch in value if ord(ch) <= 0xFFFF).strip()[:100]


def number_type(params: str) -> str:
    params = params.lower()
    for vcard, cm in (("cell", "cell"), ("iphone", "cell"), ("work", "work"), ("home", "home")):
        if f"type={vcard}" in params:
            return cm
    return "other"


def contacts(text: str, blocked: list[str] = ()) -> list[dict]:
    """Contact Manager entries for every contact with a usable number, leaving out
    blocked ones (by name or number). A number only goes to its first contact."""
    blocked_numbers = {national(b) for b in blocked} - {None}
    blocked_names = {b.strip().casefold() for b in blocked if national(b) is None and b.strip()}
    entries: list[dict] = []
    seen: set[str] = set()
    card: dict = {}
    for line in unfold(text):
        key, _, value = line.partition(":")
        field = key.split(";")[0].split(".")[-1].upper()  # "item1.TEL;type=CELL" -> TEL
        if field == "BEGIN":
            card = {"fn": "", "n": [], "org": "", "title": "", "tels": []}
        elif field == "FN":
            card["fn"] = unescape(value)
        elif field == "N":
            card["n"] = [unescape(p) for p in value.split(";")]
        elif field == "ORG":
            card["org"] = unescape(value.split(";")[0])
        elif field == "TITLE":
            card["title"] = unescape(value)
        elif field == "TEL" and "fax" not in key.lower():  # faxes never call
            card["tels"].append((value, number_type(key)))
        elif field == "END" and card:
            last, first = (card["n"] + ["", ""])[:2]
            name = card["fn"] or " ".join(p for p in (first, last) if p) or card["org"]
            numbers = []
            for raw, kind in card["tels"]:
                number = national(raw)
                if number and number not in {n["number"] for n in numbers}:
                    numbers.append({"number": number, "type": kind})
            if not numbers or name.casefold() in blocked_names or {n["number"] for n in numbers} & blocked_numbers:
                continue
            numbers = [n for n in numbers if n["number"] not in seen]
            if not numbers:
                continue
            seen.update(n["number"] for n in numbers)
            entries.append({
                "displayname": name or numbers[0]["number"],
                "fname": first, "lname": last, "company": card["org"], "title": card["title"],
                "numbers": numbers,
            })
    return entries


def main() -> None:
    if len(sys.argv) not in (2, 3):
        sys.exit(__doc__)
    blocked: list[str] = []
    if len(sys.argv) == 3:
        with open(sys.argv[2], encoding="utf-8-sig") as f:
            blocked = [line for line in f.read().splitlines() if line.strip()]
    with open(sys.argv[1], encoding="utf-8-sig", errors="replace") as f:
        entries = contacts(f.read(), blocked)
    json.dump(entries, sys.stdout)  # ASCII escapes: safe on any console encoding
    print(f"{len(entries)} contacts, {sum(len(e['numbers']) for e in entries)} numbers", file=sys.stderr)


if __name__ == "__main__":
    main()
