import io
import json
from pathlib import Path
import stat
import sys
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'action/preview'))
import github as transport
from render import file_comment, file_key


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


def test_duplicate_basenames_have_distinct_comments_and_long_passages_are_bounded():
    assert file_key('one/contract.docx') != file_key('two/contract.docx')
    item = {'sha': 'a' * 40, 'run_url': 'https://github.com/o/r/actions/runs/1'}
    record = {'path': 'one/contract.docx', 'status': 'modified', 'redline': 'one.docx', 'revisions': 900}
    changes = [{'id': f'change-{i}', 'number': i, 'kind': 'Text', 'markup': '<ins>' + 'x' * 1000 + '</ins>'} for i in range(90)]
    comment = file_comment(item, record, 'https://o.github.io/r/', changes, [])
    assert len(comment) < 48000
    assert 'of 90 changed passages' in comment
    assert 'Read all 90' not in comment
    assert 'Continue through all passages' in comment


def test_post_does_not_touch_user_comments_and_updates_existing_bot_comment(tmp_path, monkeypatch):
    monkeypatch.setenv('GITHUB_REPOSITORY', 'o/r')
    monkeypatch.delenv('GITHUB_STEP_SUMMARY', raising=False)
    body = '<!-- docx-redlines-preview:index -->\nnew preview'
    path = tmp_path / 'comments.json'
    path.write_text(json.dumps([{'pr': 1, 'sha': 'a' * 40, 'comments': [{'key': 'index', 'body': body}]}]))
    monkeypatch.setattr(transport, 'pages', lambda endpoint: [
        {'id': 10, 'body': '<!-- docx-redlines-preview:index -->\nuser', 'user': {'login': 'human'}},
        {'id': 11, 'body': '<!-- docx-redlines-preview:index -->\nold', 'user': {'login': 'github-actions[bot]'}},
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
    path.write_text(json.dumps([{'pr': 1, 'sha': 'a' * 40, 'comments': []}]))
    monkeypatch.setattr(transport, 'api', lambda endpoint: {'state': 'open', 'head': {'sha': 'b' * 40}})
    monkeypatch.setattr(transport, 'pages', lambda endpoint: pytest.fail('No comments should be read or changed'))
    transport.post(path, 'https://o.github.io/r/')
