"""The address book: matching numbers as they actually arrive, and surviving real exports."""
import asyncio
import csv
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from control.app import contacts, store

# Reserved ranges only: +44 7700 900xxx (Ofcom drama numbers) and NANP 555.
UK = "+44 7700 900123"
UK_NATIONAL = "07700 900123"
UK_INTERNATIONAL_00 = "00447700900123"
US = "+1 555 0100"
# The country this gateway's SIM is in, as egress.line_country would answer it.
GB = "gb"


class MatchingTests(unittest.TestCase):
    def test_the_same_number_matches_however_the_network_spells_it(self):
        key = contacts.number_key(UK, "gb")
        self.assertEqual(contacts.number_key(UK_NATIONAL, "gb"), key)
        self.assertEqual(contacts.number_key(UK_INTERNATIONAL_00, "gb"), key)
        self.assertEqual(contacts.number_key("+44 (0)7700 900123", "gb"), key)
        self.assertEqual(key, "+447700900123")

    def test_different_numbers_do_not_match(self):
        self.assertNotEqual(contacts.number_key(UK, "gb"),
                            contacts.number_key("+44 7700 900124", "gb"))

    def test_an_international_spelling_needs_no_country_to_be_understood(self):
        self.assertEqual(contacts.number_key(UK), "+447700900123")
        # "00" is one country's way of dialling out, not every country's, so without a country
        # neither it nor a national spelling can be placed: both stay as their digits.
        self.assertEqual(contacts.number_key(UK_INTERNATIONAL_00), "00447700900123")
        self.assertEqual(contacts.number_key(UK_NATIONAL), "07700900123")
        self.assertEqual(contacts.number_key(UK_INTERNATIONAL_00, "gb"), "+447700900123")

    def test_a_short_national_number_is_reconciled_with_its_international_form(self):
        # The case a comparison on trailing digits gets wrong: the subscriber number is short
        # enough that the country code would sit inside the compared window.
        key = contacts.number_key("+86 20 10000", "cn")
        self.assertEqual(contacts.number_key("020 10000", "cn"), key)
        self.assertEqual(key, "+862010000")

    def test_two_numbers_of_different_countries_never_collide(self):
        # These two share their last eight digits and are not the same destination.
        self.assertNotEqual(contacts.number_key("+86 20 10000", "cn"),
                            contacts.number_key("+44 20 6201 0000", "cn"))

    def test_a_number_that_only_means_something_locally_is_left_alone(self):
        # 10000 dialled in one area and in another are different destinations, and a SIM
        # carries a country but never an area code -- so these must not be reconciled.
        self.assertEqual(contacts.number_key("10000", "cn"), "10000")
        self.assertNotEqual(contacts.number_key("10000", "cn"),
                            contacts.number_key("020 10000", "cn"))
        # A subscriber number with its area code left off is the same problem.
        self.assertNotEqual(contacts.number_key("87654321", "cn"),
                            contacts.number_key("020 87654321", "cn"))
        self.assertEqual(contacts.number_key(""), "")
        self.assertEqual(contacts.number_key("not a number"), "")

    def test_a_national_spelling_belongs_to_one_country_only(self):
        # The same digits are a different destination in each country, so the country has to
        # be known; without one the spelling is left alone rather than guessed at.
        self.assertEqual(contacts.number_key("020 87654321", "cn"), "+862087654321")
        self.assertEqual(contacts.number_key("020 87654321", "gb"), "+442087654321")
        self.assertEqual(contacts.number_key("020 87654321", ""), "02087654321")
        # An international spelling is the same wherever it is read.
        for region in ("cn", "gb", ""):
            self.assertEqual(contacts.number_key(UK, region), "+447700900123")

    def test_a_contact_needs_a_usable_number(self):
        with self.assertRaises(contacts.ContactError):
            contacts.normalize_contact({"name": "Nobody", "numbers": []})
        with self.assertRaises(contacts.ContactError):
            contacts.normalize_contact({"name": "Nobody", "numbers": [{"number": "n/a"}]})
        # A card with only a number is named after it rather than refused.
        self.assertEqual(contacts.normalize_contact({"numbers": [UK]})["name"], UK)

    def test_one_contact_keeps_each_spelling_but_not_the_same_one_twice(self):
        clean = contacts.normalize_contact({"name": "A", "numbers": [UK, UK_NATIONAL, US, UK]})
        self.assertEqual([n["number"] for n in clean["numbers"]], [UK, UK_NATIONAL, US])


class VCardTests(unittest.TestCase):
    def test_a_three_point_oh_card_is_read_with_its_labels(self):
        card = ("BEGIN:VCARD\r\nVERSION:3.0\r\nN:Smith;Alice;;;\r\nFN:Alice Smith\r\n"
                "ORG:Example Ltd\r\nTEL;TYPE=CELL:" + UK + "\r\nTEL;TYPE=WORK,VOICE:" + US +
                "\r\nNOTE:two numbers\r\nEND:VCARD\r\n")
        read, problems = contacts.parse_vcard(card)
        self.assertEqual(problems, [])
        self.assertEqual(read[0]["name"], "Alice Smith")
        self.assertEqual(read[0]["company"], "Example Ltd")
        self.assertEqual([n["label"] for n in read[0]["numbers"]], ["CELL", "WORK"])

    def test_a_four_point_oh_card_and_a_folded_line_are_read(self):
        card = ("BEGIN:VCARD\nVERSION:4.0\nFN:A Very Long Name That The Exporter\n  Wrapped\n"
                "TEL;TYPE=\"cell\";VALUE=uri:tel:" + UK + "\nEND:VCARD\n")
        read, problems = contacts.parse_vcard(card)
        self.assertEqual(problems, [])
        self.assertEqual(read[0]["name"], "A Very Long Name That The Exporter Wrapped")
        self.assertEqual(read[0]["numbers"][0]["number"], UK)
        self.assertEqual(read[0]["numbers"][0]["label"], "cell")

    def test_a_card_without_a_name_falls_back_to_the_structured_one(self):
        read, _ = contacts.parse_vcard(
            "BEGIN:VCARD\nVERSION:3.0\nN:Smith;Alice;;;\nTEL:" + UK + "\nEND:VCARD\n")
        self.assertEqual(read[0]["name"], "Alice Smith")

    def test_a_card_that_cannot_be_used_is_reported_not_dropped(self):
        read, problems = contacts.parse_vcard(
            "BEGIN:VCARD\nVERSION:3.0\nFN:No Number\nEND:VCARD\n"
            "BEGIN:VCARD\nVERSION:3.0\nFN:Fine\nTEL:" + UK + "\nEND:VCARD\n")
        self.assertEqual([c["name"] for c in read], ["Fine"])
        self.assertIn("No Number", problems[0])

    def test_an_unfinished_file_says_so(self):
        _, problems = contacts.parse_vcard("BEGIN:VCARD\nFN:Cut off\nTEL:" + UK + "\n")
        self.assertTrue(problems)

    def test_an_android_two_point_one_name_in_quoted_printable_is_decoded(self):
        # Android's default export: every non-ASCII name is quoted-printable UTF-8, and a long
        # one is wrapped with a soft break ("=" at the end of the line, no indent).
        card = ("BEGIN:VCARD\r\nVERSION:2.1\r\n"
                "N;CHARSET=UTF-8;ENCODING=QUOTED-PRINTABLE:=E5=BC=A0;=E4=B8=89;;;\r\n"
                "FN;CHARSET=UTF-8;ENCODING=QUOTED-PRINTABLE:=E5=BC=A0=\r\n=E4=B8=89\r\n"
                "TEL;CELL:" + UK + "\r\nEND:VCARD\r\n")
        read, problems = contacts.parse_vcard(card)
        self.assertEqual(problems, [])
        self.assertEqual(read[0]["name"], "张三")
        self.assertEqual(read[0]["numbers"][0]["label"], "CELL")
        # The character set is the one the card declares, and one nobody knows is refused
        # rather than guessed at.
        read, _ = contacts.parse_vcard(
            "BEGIN:VCARD\nVERSION:2.1\nFN;CHARSET=GB2312;QUOTED-PRINTABLE:=D5=C5=C8=FD\n"
            "TEL:" + UK + "\nEND:VCARD\n")
        self.assertEqual(read[0]["name"], "张三")
        read, problems = contacts.parse_vcard(
            "BEGIN:VCARD\nVERSION:2.1\nFN;CHARSET=X-NONE;ENCODING=QUOTED-PRINTABLE:=D5=C5\n"
            "TEL:" + UK + "\nEND:VCARD\n")
        self.assertEqual(read[0]["name"], UK)
        self.assertIn("character set", problems[0])

    def test_a_byte_order_mark_does_not_hide_the_first_card(self):
        card = ("\ufeffBEGIN:VCARD\r\nVERSION:3.0\r\nFN:Alice\r\nTEL:" + UK +
                "\r\nEND:VCARD\r\n")
        for name in ("contacts.vcf", "contacts"):
            read, problems = contacts.parse(card, name)
            self.assertEqual(([c["name"] for c in read], problems), (["Alice"], []), name)

    def test_grouped_properties_are_read_and_their_apple_labels_used(self):
        # iOS, iCloud and Google: a number in a group, its label in the same group.
        card = ("BEGIN:VCARD\nVERSION:3.0\nFN:Alice\n"
                "item1.TEL;type=pref:" + UK + "\nitem1.X-ABLabel:_$!<Mobile>!$_\n"
                "item2.TEL:" + US + "\nitem2.X-ABLabel:Boat\n"
                "item3.TEL;type=HOME:+1 555 0101\nEND:VCARD\n")
        read, problems = contacts.parse_vcard(card)
        self.assertEqual(problems, [])
        self.assertEqual([(n["label"], n["number"]) for n in read[0]["numbers"]],
                         [("Mobile", UK), ("Boat", US), ("HOME", "+1 555 0101")])

    def test_how_a_value_is_written_is_not_taken_for_its_label(self):
        read, _ = contacts.parse_vcard(
            "BEGIN:VCARD\nVERSION:4.0\nFN:A\nTEL;VALUE=uri;TYPE=cell:tel:" + UK +
            "\nTEL;VALUE=uri:tel:" + US + "\nEND:VCARD\n")
        self.assertEqual([n["label"] for n in read[0]["numbers"]], ["cell", ""])

    def test_escapes_are_undone_in_one_pass(self):
        read, _ = contacts.parse_vcard(
            "BEGIN:VCARD\nVERSION:3.0\nFN:A\\\\nB\\, C\\nD\nTEL:" + UK + "\nEND:VCARD\n")
        self.assertEqual(read[0]["name"], "A\\nB, C\nD")

    def test_a_file_of_nothing_but_continuation_lines_is_read_in_linear_time(self):
        card = ("BEGIN:VCARD\nVERSION:3.0\nNOTE:x\n" + " y\n" * 1_000_000 +
                "FN:Alice\nTEL:" + UK + "\nEND:VCARD\n")
        started = time.monotonic()
        read, _ = contacts.parse_vcard(card)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(read[0]["note"], ("x" + "y" * 1_000_000)[:contacts.NOTE_MAX])

    def test_a_carriage_return_cannot_start_a_property_of_its_own(self):
        exported = contacts.to_vcard([{"name": "Alice\rTEL:+1 555 0199",
                                       "numbers": [{"number": UK}]}])
        self.assertNotIn("\rTEL", exported)
        read, _ = contacts.parse_vcard(exported)
        self.assertEqual([n["number"] for n in read[0]["numbers"]], [UK])
        self.assertEqual(read[0]["name"], "Alice\nTEL:+1 555 0199")

    def test_export_and_import_return_the_same_book(self):
        book = [{"name": "Alice; Smith", "company": "Example, Ltd", "note": "line\nbreak",
                 "numbers": [{"label": "CELL", "number": UK}, {"label": "", "number": US}]}]
        read, problems = contacts.parse_vcard(contacts.to_vcard(book))
        self.assertEqual(problems, [])
        self.assertEqual(read[0]["name"], "Alice; Smith")
        self.assertEqual(read[0]["company"], "Example, Ltd")
        self.assertEqual([n["number"] for n in read[0]["numbers"]], [UK, US])


class CsvTests(unittest.TestCase):
    def test_columns_are_recognised_by_their_common_names(self):
        read, problems = contacts.parse_csv(
            f"Name,Phone 1 - Value,Phone 1 - Type\r\nAlice,{UK},Mobile\r\n")
        self.assertEqual(problems, [])
        self.assertEqual(read[0]["name"], "Alice")
        self.assertEqual(read[0]["numbers"], [{"label": "Mobile", "number": UK}])

    def test_consecutive_rows_of_one_contact_are_read_as_one(self):
        read, _ = contacts.parse_csv(f"name,number\r\nAlice,{UK}\r\nAlice,{US}\r\n")
        self.assertEqual(len(read), 1)
        self.assertEqual([n["number"] for n in read[0]["numbers"]], [UK, US])

    def test_rows_apart_under_one_name_are_two_contacts(self):
        read, _ = contacts.parse_csv("name,number\nAlice," + UK + "\nBob," + US +
                                     "\nAlice,+1 555 0101\n")
        self.assertEqual([(c["name"], len(c["numbers"])) for c in read],
                         [("Alice", 1), ("Bob", 1), ("Alice", 1)])

    def test_a_file_without_a_number_column_is_refused_with_a_reason(self):
        with self.assertRaises(contacts.ContactError):
            contacts.parse_csv("first,last\r\nAlice,Smith\r\n")

    def test_export_round_trips(self):
        book = [{"name": "Alice", "company": "", "note": "",
                 "numbers": [{"label": "cell", "number": UK}]}]
        read, _ = contacts.parse_csv(contacts.to_csv(book))
        self.assertEqual(read[0]["numbers"], [{"label": "cell", "number": UK}])

    def test_an_export_cannot_carry_a_formula_into_a_spreadsheet(self):
        book = [{"name": "=HYPERLINK(\"http://example.invalid\")", "company": "@SUM(A1)",
                 "note": "-2+3", "numbers": [{"label": "+cell", "number": UK},
                                              {"label": "", "number": "=1+2 555"}]}]
        rows = list(csv.reader(io.StringIO(contacts.to_csv(book))))[1:]
        self.assertEqual(rows[0], ["'=HYPERLINK(\"http://example.invalid\")", UK, "'+cell",
                                   "'@SUM(A1)", "'-2+3"])
        self.assertEqual(rows[1][1], "'=1+2 555")
        # And it reads back as it was written.
        read, _ = contacts.parse_csv(contacts.to_csv(book))
        self.assertEqual((read[0]["name"], read[0]["company"], read[0]["note"]),
                         (book[0]["name"], "@SUM(A1)", "-2+3"))
        self.assertEqual(read[0]["numbers"], book[0]["numbers"])

    def test_the_format_is_chosen_by_content_not_by_the_file_name(self):
        card = "BEGIN:VCARD\nVERSION:3.0\nFN:Alice\nTEL:" + UK + "\nEND:VCARD\n"
        read, _ = contacts.parse(card, "contacts.csv")
        self.assertEqual(read[0]["name"], "Alice")


class _BookTest(unittest.TestCase):
    """A fresh history database for each test."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.patch = patch.multiple(store, DATA_DIR=str(root),
                                    DB_PATH=str(root / "mdd-sim-gateway.sqlite"),
                                    PREVIOUS_DB_PATH=str(root / "vowifi.sqlite"))
        self.patch.start()
        store.init()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()


class StoreTests(_BookTest):
    def test_a_contact_is_stored_and_read_back_as_typed(self):
        created = store.contact_create(
            2, {"name": "Alice", "numbers": [{"number": UK_NATIONAL}]}, GB)
        self.assertEqual(store.contact_get(2, created["id"])["numbers"][0]["number"], UK_NATIONAL)

    def test_every_read_and_write_is_scoped_to_the_owner(self):
        mine = store.contact_create(2, {"name": "Alice", "numbers": [UK]}, GB)
        store.contact_create(3, {"name": "Bob", "numbers": [US]}, GB)
        self.assertEqual([c["name"] for c in store.contacts_list(2)], ["Alice"])
        self.assertIsNone(store.contact_get(3, mine["id"]))
        self.assertFalse(store.contact_delete(3, mine["id"]))
        self.assertEqual(store.contacts_resolve(3, [UK], "gb"), {})

    def test_a_number_is_resolved_in_the_spelling_the_caller_used(self):
        store.contact_create(2, {"name": "Alice", "numbers": [{"number": UK}]}, GB)
        resolved = store.contacts_resolve(2, [UK_NATIONAL, UK_INTERNATIONAL_00, US], "gb")
        self.assertEqual(resolved[UK_NATIONAL]["name"], "Alice")
        self.assertEqual(resolved[UK_INTERNATIONAL_00]["name"], "Alice")
        self.assertNotIn(US, resolved)

    def test_search_finds_a_contact_by_name_or_by_trailing_digits(self):
        store.contact_create(2, {"name": "Alice Smith", "company": "Example",
                                 "numbers": [{"number": UK}]})
        for query in ("alice", "Example", "900123", UK_NATIONAL):
            with self.subTest(query=query):
                self.assertEqual(len(store.contacts_list(2, query, regions=GB)), 1, query)
        self.assertEqual(store.contacts_list(2, "bob", regions=GB), [])

    def test_editing_replaces_the_numbers_and_deleting_takes_them_with_it(self):
        created = store.contact_create(2, {"name": "Alice", "numbers": [UK]}, GB)
        store.contact_update(2, created["id"], {"name": "Alice S", "numbers": [US]}, GB)
        self.assertEqual(store.contacts_resolve(2, [UK], "gb"), {})
        self.assertEqual(store.contacts_resolve(2, [US], "gb")[US]["name"], "Alice S")
        self.assertTrue(store.contact_delete(2, created["id"]))
        self.assertEqual(store.contacts_resolve(2, [US], "gb"), {})
        self.assertEqual(store.contacts_count(2), 0)

    def test_importing_the_same_export_twice_does_not_double_the_book(self):
        card = ("BEGIN:VCARD\nVERSION:3.0\nFN:Alice\nTEL;TYPE=CELL:" + UK + "\nEND:VCARD\n"
                "BEGIN:VCARD\nVERSION:3.0\nFN:Bob\nTEL:" + US + "\nEND:VCARD\n")
        parsed, _ = contacts.parse_vcard(card)
        self.assertEqual(store.contacts_import(2, parsed, GB), {"added": 2, "skipped": 0})
        self.assertEqual(store.contacts_import(2, parsed, GB), {"added": 0, "skipped": 2})
        self.assertEqual(store.contacts_count(2), 2)

    def test_an_import_adds_everything_that_is_not_an_exact_copy(self):
        # The import does not decide who is the same person. Each of these differs from Alice
        # in one field, as written, and each is added as it is.
        alice = {"name": "Alice", "company": "", "note": "",
                 "numbers": [{"label": "CELL", "number": UK}]}
        store.contacts_import(2, [alice], GB)
        variants = [
            {**alice, "name": "alice"},
            {**alice, "name": "Bob"},                                   # shares her number
            {**alice, "company": "Example"},
            {**alice, "numbers": [{"label": "CELL", "number": UK_NATIONAL}]},
            {**alice, "numbers": [{"label": "", "number": UK}]},
            {**alice, "numbers": alice["numbers"] + [{"label": "", "number": US}]},
        ]
        self.assertEqual(store.contacts_import(2, variants, GB), {"added": 6, "skipped": 0})
        self.assertEqual(store.contacts_import(2, [alice, *variants], GB),
                         {"added": 0, "skipped": 7})
        self.assertEqual(store.contacts_count(2), 7)

    def test_a_national_spelling_is_reconciled_through_the_line_s_country(self):
        # The case that made the trailing-digit comparison untenable: short enough that the
        # country code would have sat inside the compared window.
        store.contact_create(2, {"name": "Support", "numbers": ["+86 20 10000"]}, "cn")
        self.assertEqual(store.contacts_resolve(2, ["020 10000"], "cn")["020 10000"]["name"],
                         "Support")
        # Bare, it is a different destination in every area, and stays unresolved.
        self.assertNotIn("10000", store.contacts_resolve(2, ["10000"], "cn"))
        # And it belongs to that country: the same digits on a line elsewhere are not it.
        self.assertEqual(store.contacts_resolve(2, ["020 10000"], "gb"), {})

    def test_a_contact_typed_nationally_belongs_to_the_gateway_s_own_country(self):
        store.contact_create(2, {"name": "广州办公室", "numbers": ["020 87654321"]}, "cn")
        found = store.contacts_resolve(2, ["+86 20 8765 4321"], "cn")
        self.assertEqual(found["+86 20 8765 4321"]["name"], "广州办公室")
        # The same national digits in another country are another number, and saying so is the
        # whole point: announcing a London caller under this name would be worse than silence.
        self.assertEqual(store.contacts_resolve(2, ["+44 20 8765 4321"], "gb"), {})

    def test_without_a_country_a_national_spelling_matches_only_itself(self):
        # What a gateway that does not know its country yet gets: no guess in any direction.
        store.contact_create(2, {"name": "Alice", "numbers": ["020 87654321"]}, "")
        self.assertEqual(store.contacts_resolve(2, ["020 87654321"], "")["020 87654321"]["name"],
                         "Alice")
        self.assertEqual(store.contacts_resolve(2, ["+86 20 8765 4321"], "cn"), {})

    def test_a_book_typed_before_the_country_was_known_is_rekeyed_once_it_is(self):
        # Imported before any SIM said where the gateway is: the national spelling is kept as
        # written, and an arriving international number cannot find it.
        store.contact_create(2, {"name": "Alice", "numbers": [UK_NATIONAL]}, ())
        store.contact_create(2, {"name": "Bob", "numbers": [US]}, ())
        self.assertEqual(store.contacts_resolve(2, [UK], GB), {})
        # Once the country is known the keys are worked out again from the number as typed;
        # the international number did not depend on it and is left alone.
        self.assertEqual(store.contacts_rekey([GB]), 1)
        self.assertEqual(store.contacts_resolve(2, [UK], GB)[UK]["name"], "Alice")
        self.assertEqual(store.contacts_rekey([GB]), 0)
        # A second country adds its own keys and leaves the first country's working.
        store.contacts_rekey(["cn", GB])
        self.assertEqual(store.contacts_resolve(2, [UK], GB)[UK]["name"], "Alice")
        self.assertEqual(store.contacts_resolve(2, [US], "cn")[US]["name"], "Bob")
        # And with no line left in that country, the national spelling matches only itself.
        store.contacts_rekey(["cn"])
        self.assertEqual(store.contacts_resolve(2, [UK], GB), {})

    def test_with_lines_in_several_countries_each_line_reads_the_book_as_its_own(self):
        # A British and a Chinese SIM in one gateway, and a book typed in national form, as a
        # phone exports it. Each line names callers the way a phone holding that SIM would.
        regions = ("cn", GB)
        store.contact_create(2, {"name": "Alice", "numbers": [UK_NATIONAL]}, regions)
        store.contact_create(2, {"name": "妈妈", "numbers": ["138 0013 8000"]}, regions)
        store.contact_create(2, {"name": "广州办公室", "numbers": ["020 8765 4321"]}, regions)
        self.assertEqual(store.contacts_resolve(2, [UK], GB)[UK]["name"], "Alice")
        self.assertEqual(store.contacts_resolve(2, ["+86 138 0013 8000"], "cn")
                         ["+86 138 0013 8000"]["name"], "妈妈")
        self.assertEqual(store.contacts_resolve(2, ["+86 20 8765 4321"], "cn")
                         ["+86 20 8765 4321"]["name"], "广州办公室")
        # A British number is not somebody in China because the digits happen to fit there,
        # and the other way round.
        self.assertEqual(store.contacts_resolve(2, ["+44 20 8765 4321"], "cn"), {})
        self.assertEqual(store.contacts_resolve(2, [UK], "cn"), {})
        self.assertEqual(store.contacts_resolve(2, ["+86 138 0013 8000"], GB), {})
        # Searching finds a contact through any of the gateway's countries.
        self.assertEqual([c["name"] for c in store.contacts_list(2, UK, regions=regions)],
                         ["Alice"])

    def test_the_book_is_bounded(self):
        with patch.object(contacts, "MAX_CONTACTS_PER_OWNER", 2):
            store.contact_create(2, {"name": "A", "numbers": [UK]}, GB)
            store.contact_create(2, {"name": "B", "numbers": [US]}, GB)
            with self.assertRaises(contacts.ContactError):
                store.contact_create(2, {"name": "C", "numbers": ["+1 555 0101"]}, GB)


class ApiTests(_BookTest):
    """The endpoints' own part: keeping the keys current, and bounding what an import reads."""

    def setUp(self):
        super().setUp()
        from control.app import main
        self.main = main
        main._contact_keys_for = None

    def request(self, body: bytes, content_type: str, *, declared=None, chunk=4096):
        headers = [(b"content-type", content_type.encode())]
        if declared is not None:
            headers.append((b"content-length", str(declared).encode()))
        chunks = [body[i:i + chunk] for i in range(0, len(body), chunk)] or [b""]
        sent = []

        async def receive():
            sent.append(chunks[len(sent)])
            return {"type": "http.request", "body": sent[-1],
                    "more_body": len(sent) < len(chunks)}

        scope = {"type": "http", "method": "POST", "headers": headers,
                 "state": {"admin_session": {"user": "admin"}}}
        return self.main.Request(scope, receive), sent

    def test_the_keys_are_worked_out_again_only_when_the_countries_change(self):
        answers = iter([(), ("gb",), ("gb",), ("cn", "gb")])
        with patch.object(self.main, "_contact_regions", lambda: next(answers)), \
                patch.object(store, "contacts_rekey") as rekey:
            for _ in range(4):
                self.main._contact_regions_current()
        self.assertEqual([c.args for c in rekey.call_args_list],
                         [((),), (("gb",),), (("cn", "gb"),)])

    def test_a_line_is_in_its_sim_s_country_wherever_its_traffic_leaves(self):
        # 234 is the United Kingdom; the exit is only where the tunnel comes out.
        self.assertEqual(self.main._line_country({"mcc": "234", "proxy_country": "us"}), "gb")
        self.assertEqual(self.main._line_country({"mcc": "", "proxy_country": "us"}), "us")
        # A resolve without a line borrows the gateway's country only when there is one.
        self.assertEqual(self.main._line_region("", ("gb",)), "gb")
        self.assertEqual(self.main._line_region("", ("cn", "gb")), "")

    def test_an_import_without_a_content_length_is_read(self):
        card = ("BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Alice\r\nTEL:" + UK +
                "\r\nEND:VCARD\r\n").encode()
        body = (b"--b\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.vcf\"\r\n"
                b"Content-Type: text/vcard\r\n\r\n" + card + b"\r\n--b--\r\n")
        request, _ = self.request(body, "multipart/form-data; boundary=b")
        with patch.object(self.main, "_contact_regions", lambda: (GB,)):
            result = asyncio.run(self.main.api_contacts_import(request))
        self.assertEqual((result["read"], result["added"]), (1, 1))

    def test_an_import_is_cut_off_once_it_passes_the_limit_whatever_it_declared(self):
        body = b'{"text": "' + b"x" * (self.main.CONTACT_IMPORT_LIMIT + 400_000) + b'"}'
        for declared in (None, 100):          # none at all, or one that understates it
            request, sent = self.request(body, "application/json", declared=declared,
                                         chunk=64 * 1024)
            with self.assertRaises(self.main.HTTPException) as refused:
                asyncio.run(self.main.api_contacts_import(request))
            self.assertEqual(refused.exception.status_code, 413, declared)
            self.assertLess(sum(map(len, sent)), len(body), declared)
        request, sent = self.request(b"{}", "application/json",
                                     declared=self.main.CONTACT_IMPORT_LIMIT * 2)
        with self.assertRaises(self.main.HTTPException) as refused:
            asyncio.run(self.main.api_contacts_import(request))
        self.assertEqual((refused.exception.status_code, sent), (413, []))


    def test_input_that_is_not_an_address_book_is_refused_with_a_4xx(self):
        field = b"name,number\r\n" + b"a" * 200_000 + b",1\r\n"
        cases = [(b"{not json", "application/json"), (b"[1]", "application/json"),
                 (b'{"text": ' + json.dumps(field.decode()).encode() + b'}', "application/json")]
        for body, content_type in cases:
            request, _ = self.request(body, content_type)
            with patch.object(self.main, "_contact_regions", lambda: (GB,)), \
                    self.assertRaises(self.main.HTTPException) as refused:
                asyncio.run(self.main.api_contacts_import(request))
            self.assertEqual(refused.exception.status_code, 400, body[:20])

    def test_the_file_is_read_off_the_event_loop(self):
        request, _ = self.request(b'{"text": "name,number\\nA,+1 555 0100"}', "application/json")
        original, threads = self.main._contact_file, []

        def read(*args):
            threads.append(threading.get_ident())
            return original(*args)

        async def run():
            threads.append(threading.get_ident())
            return await self.main.api_contacts_import(request)

        with patch.object(self.main, "_contact_regions", lambda: (GB,)), \
                patch.object(self.main, "_contact_file", read):
            self.assertEqual(asyncio.run(run())["added"], 1)
        self.assertEqual(len(threads), 2)
        self.assertNotEqual(threads[0], threads[1])

if __name__ == "__main__":
    unittest.main()
