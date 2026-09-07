"""User-visible review modes and the boundary that protects existing Pages sites."""
import json
from pathlib import Path
import sys

import pytest
from lxml import etree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'action/preview'))
import github as transport
import render
from options import Options


@pytest.mark.parametrize('values', [{'mode': 'nope'}, {'pages': 'false'}, {'preview_count': -1},
                                   {'context_paragraphs': 1.2}, {'comment_budget': 60001}])
def test_invalid_options_fail_before_publication(values):
    with pytest.raises(ValueError):
        Options(**values)


@pytest.fixture
def catalog(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    source.joinpath('diff.html').write_text('''<html xmlns="http://www.w3.org/1999/xhtml"><head/><body>
<p>Context before the deadline.</p><p>Respond within <del>30</del><ins>45</ins> days.</p>
<p>Context after the deadline.</p><p class="rev-para-format-change">Formatted provision.</p>
<p>End of context.</p></body></html>''')
    source.joinpath('latest.html').write_text('''<html xmlns="http://www.w3.org/1999/xhtml"><head/><body>
<p>The latest document only.</p><p>Respond within 45 days.</p></body></html>''')
    source.joinpath('redline.docx').write_bytes(b'redline')
    source.joinpath('latest.docx').write_bytes(b'latest')
    record = {'path': 'folder/contract.DOCX', 'status': 'modified', 'revisions': 3,
              'html': 'diff.html', 'redline': 'redline.docx', 'error': None}
    source.joinpath('manifest.json').write_text(json.dumps({'files': [record]}))
    item = {'pr': 1, 'title': 'Review a contract', 'sha': 'a' * 40, 'current_head': 'a' * 40,
            'pr_url': 'https://github.com/o/r/pull/1', 'run_url': 'https://github.com/o/r/actions/runs/1',
            'input_dir': str(source)}
    path = tmp_path / 'catalog.json'
    path.write_text(json.dumps([item]))
    return path, source, record


def build_review(tmp_path, catalog, options, monkeypatch):
    path, source, record = catalog
    monkeypatch.setattr(render, 'sync_playwright', lambda: pytest.fail('Comments must not start a browser'))
    output, comments = tmp_path / 'site', tmp_path / 'comments.json'
    render.build(path, output, '', comments, options)
    return output, json.loads(comments.read_text())


def test_default_comments_have_redlines_context_logs_and_no_pages_links(tmp_path, catalog, monkeypatch):
    output, [comment] = build_review(tmp_path, catalog, Options(), monkeypatch)
    body = comment['body']
    assert '<del>30</del><ins>45</ins>' in body
    assert 'Context before the deadline.' in body and 'Context after the deadline.' in body
    assert 'Change log' in body and 'Formatting' in body
    assert '3 revisions' in body
    assert 'github.io' not in body and '[View redline]' not in body and '![' not in body
    assert render.ARTIFACT_URL in body
    assert not list(output.rglob('*.png'))
    assert 'Formatted provision' in next(output.rglob('CHANGELOG.md')).read_text()


@pytest.mark.parametrize('mode', ['latest', 'both'])
def test_latest_and_both_generate_independent_views(tmp_path, catalog, monkeypatch, mode):
    path, source, record = catalog
    record.update(latest='latest.docx', latest_html='latest.html')
    if mode == 'latest':
        record.update(redline=None, html=None, revisions=None)
    source.joinpath('manifest.json').write_text(json.dumps({'mode': mode, 'files': [record]}))
    output, [comment] = build_review(tmp_path, catalog, Options(mode=mode), monkeypatch)
    assert any(p.read_bytes() == b'latest' for p in output.rglob('latest.docx'))
    latest_page = next(p for p in output.rglob('document.html') if 'The latest document only.' in p.read_text())
    assert '<del>' not in latest_page.read_text()
    if mode == 'latest':
        assert 'Latest document versions; no comparison was run.' in comment['body']
        assert 'The latest document only.' in comment['body']
        assert 'revisions' not in comment['body'] and '<del>' not in comment['body']
        assert 'Change log' not in comment['body']
    else:
        assert len(list(output.rglob('document.html'))) == 2
        assert '<del>30</del>' in comment['body']


def test_inline_controls_are_independent_and_reports_remain_complete(tmp_path, catalog, monkeypatch):
    output, [comment] = build_review(tmp_path, catalog,
        Options(inline_preview=False, change_log=True, max_passages=1, downloads=False), monkeypatch)
    body = comment['body']
    assert 'Context before' not in body
    assert 'Read 1 of 2 changed passages' in body
    assert 'Download results' not in body
    assert 'Formatted provision.' in next(output.rglob('CHANGELOG.md')).read_text()


def test_comments_can_be_disabled_without_suppressing_outputs(tmp_path, catalog, monkeypatch):
    output, comments = build_review(tmp_path, catalog, Options(comments=False), monkeypatch)
    assert comments == []
    assert list(output.rglob('document.html'))


def test_switching_mode_requires_a_matching_comparison(tmp_path, catalog, monkeypatch):
    with pytest.raises(ValueError, match='recompute'):
        build_review(tmp_path, catalog, Options(mode='latest'), monkeypatch)


@pytest.mark.parametrize('site_kind', ['owned', 'legacy', 'empty', 'unrelated', 'different-repo', 'unreadable'])
def test_pages_ownership_check(site_kind, monkeypatch):
    monkeypatch.setenv('GITHUB_REPOSITORY', 'o/r')
    site = {'html_url': 'https://o.github.io/r/', 'status': None if site_kind == 'empty' else 'built'}
    monkeypatch.setattr(transport, 'api', lambda path: [] if '/deployments?' in path else site)
    def read(url):
        if site_kind == 'unreadable':
            raise RuntimeError('Cannot verify ownership')
        if site_kind in ('owned', 'different-repo') and url.endswith('.json'):
            return json.dumps({'generator': 'JSv4/docx-actions', 'repository': 'o/r' if site_kind == 'owned' else 'other/repo'})
        if site_kind == 'legacy' and not url.endswith('.json'):
            return '<title>Word document previews · DOCX review</title> WORD DOCUMENT REVIEW · GITHUB ACTIONS Generated with Python-Redlines and Docxodus. Each comparison identifies its source commit.'
        return '<html>My existing documentation</html>' if site_kind == 'unrelated' and not url.endswith('.json') else None
    monkeypatch.setattr(transport, 'read_site', read)
    if site_kind in ('owned', 'legacy', 'empty'):
        transport.check_pages()
    else:
        with pytest.raises(RuntimeError):
            transport.check_pages()


def test_overwriting_an_existing_site_requires_an_explicit_override(monkeypatch):
    monkeypatch.setenv('GITHUB_REPOSITORY', 'o/r')
    monkeypatch.setattr(transport, 'api', lambda _: {'html_url': 'https://o.github.io/r/'})
    monkeypatch.setattr(transport, 'read_site', lambda _: pytest.fail('Override already authorized'))
    transport.check_pages(allow_overwrite=True)


def test_post_uses_real_artifact_url_without_a_pages_url(tmp_path, monkeypatch):
    monkeypatch.setenv('GITHUB_REPOSITORY', 'o/r')
    monkeypatch.delenv('GITHUB_STEP_SUMMARY', raising=False)
    comments = tmp_path / 'comments.json'
    comments.write_text(json.dumps([{'pr': 1, 'sha': 'a' * 40, 'file_count': 1,
        'body': '<!-- docx-redlines-preview:index -->\n[Download](' + render.ARTIFACT_URL + ')'}]))
    monkeypatch.setattr(transport, 'pages', lambda _: [])
    writes = []
    def api(path, payload=None):
        if payload:
            writes.append(payload['body'])
            return {'id': 123}
        return {'state': 'open', 'head': {'sha': 'a' * 40}}
    monkeypatch.setattr(transport, 'api', api)
    with pytest.raises(ValueError, match='artifact URL'):
        transport.post(comments)
    assert not writes
    artifact_url = 'https://github.com/o/r/actions/runs/1/artifacts/2'
    transport.post(comments, artifact_url=artifact_url)
    assert writes == [f'<!-- docx-redlines-preview:index -->\n[Download]({artifact_url})']


def test_short_context_keeps_neighboring_revisions_visible(tmp_path, catalog, monkeypatch):
    path, source, record = catalog
    source.joinpath('diff.html').write_text('''<html xmlns="http://www.w3.org/1999/xhtml"><head/><body>
<p>Before <del>old</del><ins>new</ins> wording.</p>
<p>Due in <del>30</del><ins>45</ins> days.</p><p>After.</p></body></html>''')
    changes = render.prepare_document(source/'diff.html', tmp_path/'document.html')
    excerpt = render.text_excerpt(changes[1], Options())
    assert '<em>Before <del>old</del><ins>new</ins> wording.</em>' in excerpt
    assert 'oldnew' not in excerpt


def test_comment_limit_reserves_space_for_real_download_urls():
    documents = [{'record': {'path': f'{i}/contract.docx', 'status': 'modified', 'redline': 'out.docx', 'revisions': 2},
                  'url': 'https://old.example/should-not-be-used/', 'images': [],
                  'changes': [{'number': 1, 'kind': 'Text', 'id': 'change-1', 'markup': '<ins>new wording</ins>'}]} for i in range(500)]
    body = render.review_comment({'sha': 'a'*40, 'run_url': 'https://github.com/o/r/actions/runs/1'}, documents, options=Options())
    assert len(body.replace(render.ARTIFACT_URL, 'x'*256)) <= 58000
    assert 'old.example' not in body
    assert 'of 500 documents here' in body
    assert '<details' not in body
