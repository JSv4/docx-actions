"""Check the trust boundary between a comparison artifact and published markup."""

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

from lxml import etree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'action/preview'))
from render import contextual_excerpt, neighboring_block, prepare_document, serialize_html


class PreviewTests(unittest.TestCase):
    def test_preserves_revisions_and_bookmarks_without_active_content(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "input.html"
            target = Path(directory) / "output.html"
            source.write_text('''<html xmlns="http://www.w3.org/1999/xhtml"><head><style>.rev-ins{color:green}</style></head>
<body><p id="original-bookmark" onclick="alert(1)">Within <del>30</del> <ins>45</ins> days.
<a href="javascript:alert(1)">link</a></p><script>alert(1)</script><iframe src="https://example.com"/>
<p>Unchanged text.</p><p class="rev-para-format-change">Formatted heading</p></body></html>''')
            changes = prepare_document(source, target)
            result = target.read_text()
            self.assertEqual(len(changes), 2)
            self.assertIn('<del>30</del> <ins>45</ins>', changes[0]["markup"])
            self.assertIn('id="original-bookmark"', result)
            self.assertIn('id="review-change-1"', result)
            self.assertIn('Unchanged text.', result)
            self.assertIn('Content-Security-Policy', result)
            self.assertNotIn('onclick', result)
            self.assertNotIn('javascript:', result)
            root = ET.parse(str(target), ET.HTMLParser())
            self.assertFalse(root.xpath('//*[local-name()="script" or local-name()="iframe"]'))
            self.assertEqual(changes[1]["kind"], 'Formatting')

    def test_excerpt_retains_adjacent_context_and_stays_in_its_section(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "input.html"
            target = Path(directory) / "output.html"
            source.write_text('''<html xmlns="http://www.w3.org/1999/xhtml"><head/><body>
<div><p>Unrelated prior section.</p></div><div>
<p>Prior paragraph establishes the notice requirement.</p><p> </p>
<p>Notice is due within <del>30</del> <ins>45</ins> days.</p>
<p>The following paragraph describes delivery.</p></div></body></html>''')
            [change] = prepare_document(source, target)
            result = contextual_excerpt(change, 1, 1)
            text = "".join(result.itertext())
            self.assertIn("Prior paragraph establishes", text)
            self.assertIn("following paragraph describes", text)
            self.assertIn("Expand in full document", text)
            self.assertNotIn("Unrelated prior section", text)
            self.assertEqual(len(result.xpath('//*[local-name()="ins"]')), 1)
            before = neighboring_block(change["node"], "before")
            self.assertIsNone(neighboring_block(before, "before"))

    def test_html_serialization_keeps_numbering_and_bookmarks_from_swallowing_text(self):
        root = ET.fromstring('''<html xmlns="http://www.w3.org/1999/xhtml"><head><meta charset="UTF-8"/></head>
<body><p><a id="bookmark"/><span class="number"><span>2.2</span><span data-docx-tab="left"/></span>
<span class="prose">Distribution of Remaining Assets.</span><br/>Next line.</p></body></html>''')
        serialized = serialize_html(root)
        result = ET.HTML(serialized)
        self.assertEqual(result.xpath('string(//span[@class="number"])'), '2.2')
        self.assertEqual(result.xpath('count(//span[@class="prose"]/parent::p)'), 1)
        self.assertEqual(result.xpath('count(//a[@id="bookmark"]/*)'), 0)
        self.assertEqual(result.xpath('count(//br)'), 1)
        self.assertNotIn(b'</br>', serialized)
        self.assertIn(b'<span data-docx-tab="left"></span>', serialized)


if __name__ == "__main__":
    unittest.main()
