"""Exercise the generated multi-document site in a real desktop/mobile browser."""

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
from threading import Thread

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'action/preview'))
from render import build, file_key


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def main():
    with TemporaryDirectory() as temporary:
        directory = Path(temporary)
        source, output = directory / 'input', directory / 'site'
        source.mkdir()
        prose = ''.join(f'<span>Distribution of Remaining Assets, clause {i}. </span>' for i in range(12))
        source_html = f'''<html xmlns="http://www.w3.org/1999/xhtml"><head><style>
.number{{display:inline-flex;width:0.5in;white-space:nowrap}} p{{font:16px/1.3 serif}}
</style></head><body><p>The preceding paragraph establishes the distribution rules.</p>
<p id="distribution"><a id="bookmark"/><span class="number"><span>2.2</span><span data-docx-tab="left"/></span>{prose}<del>30</del><ins>45</ins> days.</p>
<p>The following paragraph describes delivery.</p>
<p class="rev-move-to"><ins>Insurance moved to the beginning of the agreement.</ins></p>
<table><tr><td><p>Additional <ins>table provision</ins>.</p></td></tr></table>
</body></html>'''
        files = []
        for path, status in [('one/contract.docx', 'modified'), ('two/contract.docx', 'modified'), ('added.docx', 'added')]:
            key = file_key(path)
            (source / f'{key}.html').write_text(source_html)
            shutil.copyfile(ROOT / 'tests/fixtures/modified.docx', source / f'{key}.docx')
            files.append({'path': path, 'status': status, 'previous_path': None, 'revisions': 4,
                          'redline': f'{key}.docx' if status == 'modified' else None,
                          'document': f'{key}.docx' if status == 'added' else None,
                          'html': f'{key}.html', 'error': None})
        (source / 'manifest.json').write_text(json.dumps({'files': files}))
        sha = 'a' * 40
        catalog = directory / 'catalog.json'
        catalog.write_text(json.dumps([{'pr': 1, 'title': 'Review three Word documents', 'sha': sha,
            'current_head': sha, 'input_dir': str(source), 'pr_url': 'https://github.com/o/r/pull/1',
            'run_url': 'https://github.com/o/r/actions/runs/1'}]))
        comments = directory / 'comments.json'
        build(catalog, output, 'https://o.github.io/r', comments)
        [review] = json.loads(comments.read_text())
        assert len(review['comments']) == 4
        assert len({entry['key'] for entry in review['comments']}) == 4
        assert 'Added document' in review['comments'][-1]['body']
        images = list(output.rglob('preview-*.png'))
        assert len(images) == 4
        server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=str(output)))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                options = {'headless': True}
                if os.environ.get('CHROME_PATH'):
                    options['executable_path'] = os.environ['CHROME_PATH']
                browser = playwright.chromium.launch(**options)
                page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                address = f'http://127.0.0.1:{server.server_port}/pr/1/{sha}/file-{file_key("one/contract.docx")}/'
                page.goto(address + '#review-change-2')
                page.wait_for_function("document.getElementById('position').textContent.startsWith('2 of')")
                page.get_by_role('button', name='Previous changed passage').click()
                page.wait_for_function("document.getElementById('position').textContent.startsWith('1 of')")
                paragraph = page.frame_locator('#document').locator('#distribution')
                assert paragraph.locator('.number').inner_text().strip() == '2.2'
                assert paragraph.evaluate('e => e.children.length') > 10
                assert paragraph.bounding_box()['height'] < 300
                assert paragraph.locator(':scope > a#bookmark').count() == 1
                page.get_by_role('button', name='Next changed passage').click()
                assert page.url.endswith('#review-change-2')
                page.reload()
                page.wait_for_function("document.getElementById('position').textContent.startsWith('2 of')")
                page.set_viewport_size({'width': 390, 'height': 844})
                page.wait_for_timeout(250)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
                page.goto(f'http://127.0.0.1:{server.server_port}/pr/1/{sha}/')
                assert page.get_by_role('link', name='one/contract.docx', exact=True).count() == 1
                assert page.get_by_role('link', name='two/contract.docx', exact=True).count() == 1
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        print('Browser smoke test passed: 3 documents, context images, numbering, navigation, deep links, and mobile.')


if __name__ == '__main__':
    main()
