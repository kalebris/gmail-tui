"""URLs stay visible, with the word link as the part a terminal can open."""

import unittest

from gmail_tui.links import osc8, present_links, strip_link_urls
from gmail_tui.store import MessagePreview
from gmail_tui.ui import _display_width, format_reader, layout_reader, reader_hyperlinks


class LinkTextTests(unittest.TestCase):
    def test_the_address_stays_and_link_precedes_it(self) -> None:
        text = present_links("See https://example.com/a.")
        self.assertEqual(text, "See link https://example.com/a.")
        self.assertEqual(present_links(text), text)

    def test_a_wrapped_url_is_one_address(self) -> None:
        head = "https://example.com/" + ("a" * 50)
        body = head + "\n" + "bb/cc"
        self.assertGreaterEqual(len(head), 70)
        text = present_links(body)
        self.assertNotIn("\n", text)
        self.assertIn(head + "bb/cc", text)
        self.assertTrue(text.startswith("link "))

    def test_the_next_sentence_is_not_swallowed(self) -> None:
        head = "https://example.com/" + ("a" * 50)
        text = present_links(head + "\ntoday")
        self.assertIn("\n", text)
        self.assertIn("today", text.splitlines()[1])

    def test_osc8_points_at_the_url_and_shows_the_word_link(self) -> None:
        self.assertEqual(
            osc8("link", "https://example.com/a"),
            "\033]8;;https://example.com/a\033\\link\033]8;;\033\\",
        )


class LinkLayoutTests(unittest.TestCase):
    def test_a_short_url_stays_on_its_row(self) -> None:
        line = present_links("see https://example.com/a now")
        view, _starts, marks = layout_reader([line], 80)
        self.assertEqual(view, [line])
        self.assertEqual(marks[0].links, ((4, "https://example.com/a"),))

    def test_a_long_url_stays_inside_the_pane(self) -> None:
        url = "https://example.com/" + ("a" * 80)
        line = present_links(url)
        view, _starts, marks = layout_reader([line], 30)
        self.assertGreater(len(view), 1)
        self.assertTrue(all(_display_width(row) <= 30 for row in view))
        self.assertIn(url, "".join(view))
        self.assertEqual(marks[0].links, ((0, url),))
        writes = reader_hyperlinks(view, marks, 0, top=4, pane_left=10, rows=8, term=50)
        self.assertEqual(writes, [(4, 10, "link", url)])

    def test_hidden_urls_keep_only_the_word_and_its_target(self) -> None:
        line = present_links("see https://example.com/a and https://example.org/b now")
        stripped, found = strip_link_urls(line)
        self.assertEqual(stripped, "see link and link now")
        self.assertEqual(found, [(4, "https://example.com/a"), (13, "https://example.org/b")])
        view, _starts, marks = layout_reader([line], 80, hide_urls=True)
        self.assertEqual(view, ["see link and link now"])
        self.assertEqual(
            marks[0].links, ((4, "https://example.com/a"), (13, "https://example.org/b"))
        )
        writes = reader_hyperlinks(view, marks, 0, top=2, pane_left=1, rows=5, term=80)
        self.assertEqual(
            writes,
            [(2, 5, "link", "https://example.com/a"), (2, 14, "link", "https://example.org/b")],
        )

    def test_a_hidden_long_url_takes_one_word(self) -> None:
        url = "https://example.com/" + ("a" * 80)
        view, _starts, marks = layout_reader([present_links(url)], 30, hide_urls=True)
        self.assertEqual(view, ["link"])
        self.assertEqual(marks[0].links, ((0, url),))

    def test_reader_shows_the_url_after_link(self) -> None:
        preview = MessagePreview(
            gmail_id="m",
            from_addr="Ada <ada@example.com>",
            to_addr="me@gmail.com",
            cc_addr="",
            subject="Notes",
            snippet="",
            internal_date_ms=0,
            label_names=("INBOX",),
            has_attachment=False,
            body_text="See https://example.com/a for the notes.",
            body_cached=True,
        )
        lines = format_reader(preview)
        self.assertIn("See link https://example.com/a for the notes.", lines)


if __name__ == "__main__":
    unittest.main()
