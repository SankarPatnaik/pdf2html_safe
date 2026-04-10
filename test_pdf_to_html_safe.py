import unittest

from pdf_to_html_safe import (
    TableRegion,
    TextBlock,
    TextLine,
    build_list_fragment,
    detect_table_regions,
    filter_probable_watermark_text_lines,
    group_lines_into_blocks,
    is_bullet_line,
    strip_bullet_prefix,
)


class ParserHeuristicTests(unittest.TestCase):
    def mk(self, text, x0, y0, x1=None, y1=None, size=10.0):
        x1 = x1 if x1 is not None else x0 + max(20.0, len(text) * 4.0)
        y1 = y1 if y1 is not None else y0 + 10
        return TextLine(
            text=text,
            x0=float(x0),
            y0=float(y0),
            x1=float(x1),
            y1=float(y1),
            font_name="Times-Roman",
            font_size=size,
            page_width=600.0,
            page_height=800.0,
        )

    def test_bullet_detection(self):
        self.assertTrue(is_bullet_line("• First level"))
        self.assertTrue(is_bullet_line("a) Sub point"))
        self.assertTrue(is_bullet_line("1. Numbered item"))
        self.assertFalse(is_bullet_line("This is a paragraph."))
        prefix, content = strip_bullet_prefix("•  Keep this text")
        self.assertEqual(prefix, "•")
        self.assertEqual(content, "Keep this text")

    def test_indented_short_line_not_merged(self):
        lines = [
            self.mk("Main paragraph opening line.", 72, 100),
            self.mk("Indented sub paragraph", 110, 116),
            self.mk("Next paragraph", 72, 145),
        ]
        blocks = group_lines_into_blocks(lines)
        self.assertEqual(len(blocks), 3)

    def test_table_detection_requires_repeated_columns(self):
        rows = [
            [self.mk("Name", 72, 100), self.mk("Age", 220, 100), self.mk("City", 320, 100)],
            [self.mk("Alice", 72, 114), self.mk("30", 220, 114), self.mk("Pune", 320, 114)],
            [self.mk("Bob", 72, 128), self.mk("40", 220, 128), self.mk("Mumbai", 320, 128)],
        ]
        regions = detect_table_regions(rows)
        self.assertEqual(len(regions), 1)
        self.assertIsInstance(regions[0], TableRegion)

    def test_do_not_detect_table_for_bullets(self):
        rows = [
            [self.mk("• Item one", 72, 100), self.mk("extra text", 220, 100)],
            [self.mk("• Item two", 72, 114), self.mk("extra text", 220, 114)],
            [self.mk("• Item three", 72, 128), self.mk("extra text", 220, 128)],
        ]
        self.assertEqual(detect_table_regions(rows), [])

    def test_nested_list_fragment(self):
        frag = build_list_fragment([
            (0, "Root", False),
            (1, "Child", False),
            (0, "Second root", False),
        ])
        self.assertIn("<ul>", frag)
        self.assertIn("<li>Root</li>", frag)
        self.assertIn("<li>Child</li>", frag)

    def test_filter_rotated_overlay_watermark_text(self):
        body_1 = self.mk("The issue involved also is common which pertains", 72, 300, y1=314, size=12)
        body_2 = self.mk("to the valuation of goods sold by the assessee", 72, 312, y1=326, size=12)
        watermark = TextLine(
            text="JUDGMENT",
            x0=150,
            y0=306,
            x1=420,
            y1=340,
            font_name="Times-Bold",
            font_size=24,
            page_width=600,
            page_height=800,
            dir_x=0.75,
            dir_y=0.65,
        )

        filtered = filter_probable_watermark_text_lines([body_1, watermark, body_2])
        self.assertEqual([line.text for line in filtered], [body_1.text, body_2.text])


if __name__ == "__main__":
    unittest.main()
