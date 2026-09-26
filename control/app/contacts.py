"""Address-book formats and number matching, without a database in sight.

Two things here are easy to get wrong and worth stating.

**Matching.** A number arrives in whatever shape the network used: `+447700900123` from one
carrier, `07700900123` from another, `00447700900123` over a third. An address book entered by
hand is no tidier. Two spellings are compared by reducing each to E.164, which is the only form
that names one destination from anywhere.

Reducing a national spelling to E.164 needs to know which country it is national to, and the
gateway does know: ``egress.line_country`` answers it per line -- the operator's country-exit
choice first, the SIM's own MCC otherwise. Callers pass it as `region`.

Comparing trailing digits instead, as handsets do, is tempting and wrong here. It has to take
the tail of the full international digit string, so as soon as the subscriber number is short
enough the country code is inside the window: `+86 20 10000` and `020 10000` then differ, while
`+86 20 10000` and `+44 20 6201 0000` collide and one caller is announced under the other's
name. Short national numbers of exactly this shape are ordinary in several numbering plans.

A number that is only meaningful where it was dialled -- a short code, or a subscriber number
with the area code left off -- is deliberately **not** reconciled with a fuller spelling. Local
`10000`, `020 10000` and `010 10000` may be three different destinations, and the gateway has
no way to know which one was meant: its SIMs carry a country, never an area code. Such a number
is kept as its digits and is equal only to the same digits.

**Import.** A vCard from a phone can be exported by anything, so the parser stays conservative:
it reads the properties that carry a name and numbers, understands the folding and escaping the
format requires, and ignores what it does not know rather than guessing. A row that cannot be
read is reported by name, never dropped silently -- somebody importing three hundred contacts
needs to know which two did not arrive.
"""
from __future__ import annotations

import csv
import io
import quopri
import re

import phonenumbers
from phonenumbers.phonenumberutil import ValidationResult

NAME_MAX = 128
LABEL_MAX = 32
NUMBER_MAX = 40
NOTE_MAX = 512
# An address book this size is already far beyond what a gateway with five lines needs, and the
# limit is what stops one import from filling the database.
MAX_CONTACTS_PER_OWNER = 5000
MAX_NUMBERS_PER_CONTACT = 20


def digits_of(number: str) -> str:
    """Just the digits, which is what "is this a number at all" comes down to."""
    return re.sub(r"\D", "", str(number or ""))


def number_key(number: str, region: str | None = None) -> str:
    """The form that two spellings of one destination share, for a gateway sitting in `region`.

    E.164 when the number says enough to be dialled from anywhere -- it was written
    internationally, or it is a complete national number of `region`. Otherwise the digits as
    written: a number that is only meaningful locally is a different destination somewhere
    else, so it is left equal only to itself. `region` is an ISO-3166 alpha-2 code; without one
    only an international spelling can be reduced.
    """
    digits = digits_of(number)
    if not digits:
        return ""
    try:
        parsed = phonenumbers.parse(str(number), (region or "").upper() or None)
    except phonenumbers.NumberParseException:
        return digits
    # IS_POSSIBLE means the length fits a real national number of that country; the reason
    # matters because IS_POSSIBLE_LOCAL_ONLY is exactly the case that must not be reconciled.
    if phonenumbers.is_possible_number_with_reason(parsed) != ValidationResult.IS_POSSIBLE:
        return digits
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)




def clean_number(number: str) -> str:
    """A number as it will be stored and shown: trimmed, length-bounded, otherwise untouched."""
    return str(number or "").strip()[:NUMBER_MAX]


def clean_name(name: str) -> str:
    return str(name or "").strip()[:NAME_MAX]


class ContactError(ValueError):
    """A refused address-book operation whose message is safe to show the caller."""


def normalize_contact(contact: dict, region: str = "") -> dict:
    """Bound and tidy one contact, refusing the two things that make an entry meaningless."""
    name = clean_name(contact.get("name"))
    numbers = []
    seen = set()
    for item in (contact.get("numbers") or [])[:MAX_NUMBERS_PER_CONTACT]:
        if isinstance(item, str):
            item = {"number": item}
        number = clean_number((item or {}).get("number"))
        if not number or not digits_of(number):
            continue
        # Deduplicated on what matching compares, not on the exact digits: an export that
        # lists a number in both national and international form is one number.
        key = number_key(number, region)
        if key in seen:
            continue
        seen.add(key)
        numbers.append({"label": str((item or {}).get("label") or "").strip()[:LABEL_MAX],
                        "number": number})
    if not name and numbers:
        name = numbers[0]["number"]
    if not name:
        raise ContactError("a contact needs a name or a number")
    if not numbers:
        raise ContactError("a contact needs at least one number")
    return {"name": name,
            "company": str(contact.get("company") or "").strip()[:NAME_MAX],
            "note": str(contact.get("note") or "").strip()[:NOTE_MAX],
            "numbers": numbers}


# ----------------------------- vCard -----------------------------
# One pass, so an escaped backslash is never read again as the start of another escape: the
# three characters \\n in a card are a backslash followed by "n", not a newline.
_VCARD_ESCAPE = re.compile(r"\\(.)", re.S)
_VCARD_UNESCAPED = {"n": "\n", "N": "\n"}
# Apple writes its standard labels as _$!<Mobile>!$_ and a custom one as plain text.
_APPLE_LABEL = re.compile(r"^_\$!<(.*)>!\$_$")
# Parameters that describe how a value is written, never what kind of number it is.
_NOT_A_TYPE = {"VALUE", "CHARSET", "ENCODING", "PREF", "PID", "ALTID", "LANGUAGE"}


def _quoted_printable(head: str) -> bool:
    return "QUOTED-PRINTABLE" in head.upper()


def _unfold(text: str) -> list[str]:
    """Join continuation lines into the logical lines they were cut from.

    A vCard wraps a long value and continues it with one space or tab. A 2.1 value in
    quoted-printable is different: it is wrapped with a soft break, a "=" at the end of the line,
    and the next line carries on without an indent -- Android writes a long name exactly so.

    The pieces of each line are collected and joined once. Appending to a string that already
    sits in a list copies it every time, and a file of nothing but short continuation lines turns
    that into seconds of work.
    """
    lines: list[list[str]] = []
    soft_break = False
    for raw in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if lines and (soft_break or raw[:1] in (" ", "\t")):
            pieces = lines[-1]
            if soft_break:
                pieces[-1] = pieces[-1][:-1]
                pieces.append(raw)
            else:
                pieces.append(raw[1:])
        else:
            lines.append([raw])
        head = lines[-1][0].partition(":")[0]
        soft_break = _quoted_printable(head) and raw.endswith("=")
    return ["".join(pieces) for pieces in lines]


def _unescape(value: str) -> str:
    return _VCARD_ESCAPE.sub(lambda m: _VCARD_UNESCAPED.get(m.group(1), m.group(1)), value)


def _escape(value: str) -> str:
    return (str(value or "").replace("\\", "\\\\").replace("\n", "\\n")
            .replace(",", "\\,").replace(";", "\\;"))


def _property(line: str) -> tuple[str, str, list[str], str] | None:
    """(group, name, parameters, value) of one logical line.

    iOS, iCloud and Google put related properties in a group -- item1.TEL beside
    item1.X-ABLabel -- and the group is only a tie between them, not part of the name.
    """
    head, _, value = line.partition(":")
    if not _:
        return None
    parts = head.split(";")
    group, _, name = parts[0].strip().rpartition(".")
    return group.upper(), name.upper(), [p.strip() for p in parts[1:]], value


def _decode(parameters: list[str], value: str) -> str:
    """A 2.1 value as text: quoted-printable bytes in the declared character set.

    Android's default export writes every non-ASCII name this way. The character set is the one
    the card declares -- UTF-8 when it says nothing, as Android does -- and a set Python does not
    know is refused by the caller rather than guessed at, since a wrong guess stores mojibake
    under somebody's name.
    """
    upper = [p.upper() for p in parameters]
    if not any(p.startswith("ENCODING=") and "QUOTED-PRINTABLE" in p or p == "QUOTED-PRINTABLE"
               for p in upper):
        return value
    charset = next((p.partition("=")[2].strip('"') for p in parameters
                    if p.upper().startswith("CHARSET=")), "") or "utf-8"
    return quopri.decodestring(value.encode("ascii", "replace")).decode(charset, "replace")


def _type_label(parameters: list[str]) -> str:
    """The human label of a TEL property: TYPE=CELL, or the bare CELL of vCard 2.1."""
    for parameter in parameters:
        name, has_value, value = parameter.partition("=")
        if has_value and name.strip().upper() in _NOT_A_TYPE:
            continue
        candidate = (value if has_value else name).strip().strip('"')
        for word in candidate.split(","):
            word = word.strip()
            if (word.upper() not in ("PREF", "VOICE", "INTERNET", "TEL", "QUOTED-PRINTABLE", "")
                    and not word.isdigit()):
                return word[:LABEL_MAX]
    return ""


def _group_label(value: str) -> str:
    label = _unescape(value).strip()
    apple = _APPLE_LABEL.match(label)
    return (apple.group(1) if apple else label)[:LABEL_MAX]


def parse_vcard(text: str) -> tuple[list[dict], list[str]]:
    """Read one or more vCards. Returns the contacts and a message for each card refused."""
    contacts: list[dict] = []
    problems: list[str] = []
    current: dict | None = None
    for line in _unfold(text):
        parsed = _property(line.strip())
        if not parsed:
            continue
        group, name, parameters, value = parsed
        if name == "BEGIN" and value.strip().upper() == "VCARD":
            current = {"name": "", "formatted": "", "structured": "", "company": "",
                       "note": "", "numbers": [], "labels": {}}
            continue
        if current is None:
            continue
        if name == "END" and value.strip().upper() == "VCARD":
            numbers = [{"label": current["labels"].get(group) or label, "number": number}
                       for group, label, number in current["numbers"]]
            candidate = {"name": current["formatted"] or current["structured"],
                         "company": current["company"], "note": current["note"],
                         "numbers": numbers}
            try:
                contacts.append(normalize_contact(candidate))
            except ContactError as exc:
                problems.append(f"{candidate['name'] or '(unnamed)'}: {exc}")
            current = None
            continue
        try:
            value = _decode(parameters, value)
        except LookupError:
            problems.append(f"{name}: unknown character set; export as vCard 3.0")
            continue
        if name == "FN":
            current["formatted"] = _unescape(value).strip()
        elif name == "N":
            fields = [_unescape(part).strip() for part in value.split(";")]
            family, given = (fields + ["", ""])[:2]
            current["structured"] = " ".join(part for part in (given, family) if part)
        elif name == "ORG":
            current["company"] = _unescape(value.split(";")[0]).strip()
        elif name == "NOTE":
            current["note"] = _unescape(value).strip()
        elif name == "TEL":
            number = _unescape(value).strip()
            if number.lower().startswith("tel:"):
                number = number[4:]
            current["numbers"].append((group, _type_label(parameters), number))
        elif name == "X-ABLABEL" and group:
            current["labels"][group] = _group_label(value)
    if current is not None:
        problems.append("the file ends inside a card (END:VCARD is missing)")
    return contacts, problems


def to_vcard(contacts) -> str:
    """Write vCard 3.0: the version every phone and desktop address book still reads."""
    out = io.StringIO()
    for contact in contacts:
        out.write("BEGIN:VCARD\r\nVERSION:3.0\r\n")
        out.write(f"FN:{_escape(contact.get('name'))}\r\n")
        out.write(f"N:{_escape(contact.get('name'))};;;;\r\n")
        if contact.get("company"):
            out.write(f"ORG:{_escape(contact['company'])}\r\n")
        for item in contact.get("numbers") or []:
            label = str(item.get("label") or "").strip()
            type_parameter = f";TYPE={_escape(label)}" if label else ""
            out.write(f"TEL{type_parameter}:{_escape(item.get('number'))}\r\n")
        if contact.get("note"):
            out.write(f"NOTE:{_escape(contact['note'])}\r\n")
        out.write("END:VCARD\r\n")
    return out.getvalue()


# ----------------------------- CSV -----------------------------
CSV_COLUMNS = ("name", "number", "label", "company", "note")
# What the common exporters call these columns. Matched case- and space-insensitively.
_CSV_ALIASES = {
    "name": ("name", "display name", "full name", "fullname", "姓名", "名称"),
    "number": ("number", "phone", "phone number", "mobile", "mobile phone", "tel",
               "telephone", "phone 1 - value", "电话", "手机", "号码"),
    "label": ("label", "type", "phone 1 - type", "标签", "类型"),
    "company": ("company", "organization", "organisation", "org", "公司"),
    "note": ("note", "notes", "备注"),
}


def _csv_column_map(header) -> dict[str, int]:
    found: dict[str, int] = {}
    for index, cell in enumerate(header or []):
        key = str(cell or "").strip().lower().lstrip("﻿")
        for field, aliases in _CSV_ALIASES.items():
            if field not in found and key in aliases:
                found[field] = index
    return found


def parse_csv(text: str) -> tuple[list[dict], list[str]]:
    """Read a CSV address book. Rows sharing a name are folded into one contact's numbers."""
    rows = list(csv.reader(io.StringIO(str(text or ""))))
    if not rows:
        return [], []
    columns = _csv_column_map(rows[0])
    if "number" not in columns:
        raise ContactError("the file has no recognisable phone-number column")
    order: list[str] = []
    grouped: dict[str, dict] = {}
    problems: list[str] = []
    for line, row in enumerate(rows[1:], start=2):
        def cell(field):
            index = columns.get(field)
            return str(row[index]).strip() if index is not None and index < len(row) else ""
        number = cell("number")
        if not number:
            continue
        name = cell("name") or number
        key = name.casefold()
        if key not in grouped:
            grouped[key] = {"name": name, "company": cell("company"), "note": cell("note"),
                            "numbers": []}
            order.append(key)
        grouped[key]["numbers"].append({"label": cell("label"), "number": number})
        if not digits_of(number):
            problems.append(f"line {line}: '{number}' has no digits")
    contacts = []
    for key in order:
        try:
            contacts.append(normalize_contact(grouped[key]))
        except ContactError as exc:
            problems.append(f"{grouped[key]['name']}: {exc}")
    return contacts, problems


def to_csv(contacts) -> str:
    """One row per number, which is what a spreadsheet and every importer expect."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(CSV_COLUMNS)
    for contact in contacts:
        for item in contact.get("numbers") or []:
            writer.writerow([contact.get("name") or "", item.get("number") or "",
                             item.get("label") or "", contact.get("company") or "",
                             contact.get("note") or ""])
    return out.getvalue()


def parse(text: str, filename: str = "") -> tuple[list[dict], list[str]]:
    """Read whichever of the two formats this is, by content first and name second."""
    # Windows and Outlook start a file with a byte-order mark; left on, it hides the first card.
    text = str(text or "").lstrip("﻿")
    if text.lstrip()[:11].upper().startswith("BEGIN:VCARD") or filename.lower().endswith((".vcf", ".vcard")):
        return parse_vcard(text)
    return parse_csv(text)
