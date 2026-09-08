import io
import json
from pathlib import Path
import stat
import sys
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'action/preview'))
import github as transport
from lxml import etree as ET
from render import review_comment, file_key


@pytest.mark.parametrize('name', ['../escape.html', '/absolute.docx', 'a\\b.docx', 'run.py'])
def test_artifact_rejects_unsafe_files(tmp_path, name):
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as archive:
        archive.writestr(name, 'unsafe')
    with pytest.raises(ValueError):
        transport.extract_artifact(data.getvalue(), tmp_path)


def test_artifact_rejects_symlinks(tmp_path):
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as archive:
        entry = zipfile.ZipInfo('linked.html')
        entry.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(entry, '/etc/passwd')
    with pytest.raises(ValueError, match='symbolic'):
        transport.extract_artifact(data.getvalue(), tmp_path)


def test_manifest_requires_matching_repository_and_pr():
    manifest = {'schema_version': 1, 'repository': 'owner/repo', 'pr': 12,
                'base_sha': 'a' * 40, 'head_sha': 'b' * 40, 'files': []}
    transport.validate_manifest(manifest, 'owner/repo', 12)
    with pytest.raises(ValueError, match='provenance'):
        transport.validate_manifest(manifest, 'owner/repo', 13)


ITEM = {'sha': 'a' * 40, 'run_url': 'https://github.com/o/r/actions/runs/1'}
REVIEW_URL = 'https://o.github.io/r/pr/1/'


def document(path, changes=2, size=50):
    return {'record': {'path': path, 'status': 'modified', 'redline': 'out.docx', 'revisions': changes},
            'url': REVIEW_URL + file_key(path) + '/',
            'changes': [{'id': f'change-{i}', 'number': i, 'kind': 'Text', 'markup': '<ins>' + 'x' * size + '</ins>'} for i in range(changes)],
            'images': [{'anchor': 'change-1', 'name': 'preview.png', 'kind': 'Text'}]}


def test_multi_document_comment_has_direct_links_and_collapsed_previews():
    documents = [document('one/contract.docx'), document('two/contract.docx')]
    comment = review_comment(ITEM, documents, REVIEW_URL)
    root = ET.HTML(comment)
    previews = root.xpath('//details[summary[contains(., "Preview changes:")]]')
    assert len(previews) == 2
    assert all('open' not in preview.attrib for preview in previews)
    assert comment.count('Action run') == 1
    assert comment.count('Underlined: inserted') == 1
    assert comment.count('<!-- docx-redlines-preview:') == 1
    for doc in documents:
        assert f"<code>{doc['record']['path']}</code>" in comment
        assert f"[View redline]({doc['url']})" in comment
        assert f"[Word]({doc['url']}redline.docx)" in comment


def test_single_document_opens_only_its_preview():
    comment = review_comment(ITEM, [document('contract.docx')], REVIEW_URL)
    root = ET.HTML(comment)
    assert len(root.xpath('//details[@open]')) == 1
    assert root.xpath('//details[@open]/summary[contains(., "Preview changes:")]')
    assert root.xpath('//details[@open]/blockquote/details[not(@open)]')


def test_long_documents_share_comment_space_without_hiding_other_previews():
    documents = [document(f'{folder}/contract.docx', changes=90, size=1000) for folder in ('one', 'two', 'three')]
    comment = review_comment(ITEM, documents, REVIEW_URL)
    assert len(comment) <= 58000
    assert comment.count('of 90 changed passages') == 3
    assert 'Read all 90' not in comment
    assert comment.count('Continue through all passages') == 3
    assert comment.count('preceding and following context') == 3


def test_large_file_list_discloses_comment_limit_and_keeps_full_index_link():
    comment = review_comment(ITEM, [document(f'{i}/contract.docx') for i in range(500)], REVIEW_URL)
    assert len(comment) <= 58000
    assert 'of 500 documents here' in comment
    assert f'[Review all 500 documents]({REVIEW_URL})' in comment


def test_unpaired_and_failed_files_remain_visible_and_filenames_cannot_break_table():
    added = document('new|<file>\nname.docx')
    added['record'].update(status='added', redline=None)
    failed = document('failed.docx')
    failed['record']['error'] = 'render failed'
    failed['url'] = None
    comment = review_comment(ITEM, [added, failed], REVIEW_URL)
    assert 'new&#124;&lt;file&gt;&#10;name.docx' in comment
    assert '| Added | [View document]' in comment
    assert '| Failed | [See result]' in comment
    assert '<details' not in comment


@pytest.mark.parametrize('old_marker', ['<!-- docx-redlines-preview:index -->', '<!-- docxodus-inline-preview -->'])
def test_post_does_not_touch_user_comments_and_updates_existing_bot_comment(tmp_path, monkeypatch, old_marker):
    monkeypatch.setenv('GITHUB_REPOSITORY', 'o/r')
    monkeypatch.delenv('GITHUB_STEP_SUMMARY', raising=False)
    body = '<!-- docx-redlines-preview:index -->\nnew preview'
    path = tmp_path / 'comments.json'
    path.write_text(json.dumps([{'pr': 1, 'sha': 'a' * 40, 'file_count': 1, 'body': body}]))
    monkeypatch.setattr(transport, 'pages', lambda endpoint: [
        {'id': 10, 'body': '<!-- docx-redlines-preview:index -->\nuser', 'user': {'login': 'human'}},
        {'id': 11, 'body': old_marker + '\nold', 'user': {'login': 'github-actions[bot]'}},
    ])
    writes = []
    def api(endpoint, payload=None):
        if payload is not None:
            writes.append((endpoint, payload))
            return {}
        return {'state': 'open', 'head': {'sha': 'a' * 40}}
    monkeypatch.setattr(transport, 'api', api)
    transport.post(path, 'https://o.github.io/r/')
    assert writes == [('repos/o/r/issues/comments/11', {'body': body})]


def test_post_skips_newer_head(tmp_path, monkeypatch):
    monkeypatch.setenv('GITHUB_REPOSITORY', 'o/r')
    monkeypatch.delenv('GITHUB_STEP_SUMMARY', raising=False)
    path = tmp_path / 'comments.json'
    path.write_text(json.dumps([{'pr': 1, 'sha': 'a' * 40, 'file_count': 1, 'body': 'unused'}]))
    monkeypatch.setattr(transport, 'api', lambda endpoint: {'state': 'open', 'head': {'sha': 'b' * 40}})
    monkeypatch.setattr(transport, 'pages', lambda endpoint: pytest.fail('No comments should be read or changed'))
    transport.post(path, 'https://o.github.io/r/')


@pytest.mark.parametrize('replacement_succeeds', [True, False])
def test_obsolete_bot_comments_are_removed_only_after_successful_replacement(tmp_path, monkeypatch, replacement_succeeds):
    monkeypatch.setenv('GITHUB_REPOSITORY', 'o/r')
    monkeypatch.delenv('GITHUB_STEP_SUMMARY', raising=False)
    body = '<!-- docx-redlines-preview:index -->\nconsolidated'
    path = tmp_path / 'comments.json'
    path.write_text(json.dumps([{'pr': 1, 'sha': 'a' * 40, 'file_count': 3, 'body': body}]))
    old = [
        {'id': 11, 'body': '<!-- docx-redlines-preview:index -->\nold', 'user': {'login': 'github-actions[bot]'}},
        {'id': 12, 'body': '<!-- docx-redlines-preview:' + 'a' * 20 + ' -->\nold', 'user': {'login': 'github-actions[bot]'}},
        {'id': 13, 'body': '<!-- docx-redlines-preview:' + 'b' * 20 + ' -->\nhuman discussion', 'user': {'login': 'human'}},
        {'id': 14, 'body': 'Unrelated bot result', 'user': {'login': 'github-actions[bot]'}},
    ]
    monkeypatch.setattr(transport, 'pages', lambda endpoint: old)
    writes = []
    def api(endpoint, payload=None, method=None):
        if payload or method:
            writes.append((endpoint, method or 'PATCH'))
            if payload and not replacement_succeeds:
                raise RuntimeError('API unavailable')
            return {'id': 11}
        return {'state': 'open', 'head': {'sha': 'a' * 40}}
    monkeypatch.setattr(transport, 'api', api)
    if replacement_succeeds:
        transport.post(path, REVIEW_URL)
        assert writes == [('repos/o/r/issues/comments/11', 'PATCH'), ('repos/o/r/issues/comments/12', 'DELETE')]
    else:
        with pytest.raises(RuntimeError, match='unavailable'):
            transport.post(path, REVIEW_URL)
        assert writes == [('repos/o/r/issues/comments/11', 'PATCH')]


def test_delete_api_accepts_no_content_response(monkeypatch):
    calls = []
    monkeypatch.setattr(transport.subprocess, 'check_output', lambda command, **kwargs: calls.append(command) or '')
    assert transport.api('repos/o/r/issues/comments/1', method='DELETE') is None
    assert calls == [['gh', 'api', 'repos/o/r/issues/comments/1', '--method', 'DELETE']]


@pytest.mark.parametrize('scenario', ['create', 'unchanged', 'code-only', 'new-head'])
def test_comment_lifecycle(tmp_path, monkeypatch, scenario):
    monkeypatch.setenv('GITHUB_REPOSITORY', 'o/r')
    monkeypatch.delenv('GITHUB_STEP_SUMMARY', raising=False)
    body = '<!-- docx-redlines-preview:index -->\nconsolidated'
    path = tmp_path / 'comments.json'
    path.write_text(json.dumps([{'pr': 1, 'sha': 'a' * 40,
                                'file_count': 0 if scenario == 'code-only' else 3, 'body': body}]))
    old = [] if scenario in ('create', 'code-only') else [
        {'id': 11, 'body': body, 'user': {'login': 'github-actions[bot]'}},
    ]
    if scenario == 'new-head':
        old[0]['body'] += '\nold'
        old.append({'id': 12, 'body': '<!-- docx-redlines-preview:' + 'a' * 20 + ' -->',
                    'user': {'login': 'github-actions[bot]'}})
    monkeypatch.setattr(transport, 'pages', lambda endpoint: old)
    writes = []
    def api(endpoint, payload=None, method=None):
        if payload or method:
            writes.append((endpoint, method or ('PATCH' if '/issues/comments/' in endpoint else 'POST')))
            return {'id': 20}
        sha = 'b' * 40 if scenario == 'new-head' and writes else 'a' * 40
        return {'state': 'open', 'head': {'sha': sha}}
    monkeypatch.setattr(transport, 'api', api)
    transport.post(path, REVIEW_URL)
    expected = {'create': [('repos/o/r/issues/1/comments', 'POST')],
                'new-head': [('repos/o/r/issues/comments/11', 'PATCH')]}
    assert writes == expected.get(scenario, [])
