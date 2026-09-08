"""Branch publication, immutable comment URLs, and repository ownership."""
import base64
import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'action/preview'))
import github as transport
import images
from options import IMAGE_URL, Options
from render import ARTIFACT_URL, review_comment


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setenv('GITHUB_REPOSITORY', 'o/r')
    relative = 'pr/1/' + 'a'*40 + '/file-' + 'b'*20 + '/preview-1-' + 'c'*12 + '.png'
    site = tmp_path / 'site'
    picture = site / relative
    picture.parent.mkdir(parents=True)
    picture.write_bytes(b'\x89PNG\r\n\x1a\nimage')
    (site / 'private.docx').write_bytes(b'never publish Word files')
    (site / 'index.html').write_text('never publish HTML')
    comments = tmp_path / 'comments.json'
    comments.write_text(json.dumps([{'pr': 1, 'sha': 'a'*40, 'file_count': 1,
        'body': f'<!-- docx-redlines-preview:index -->\n[![preview]({IMAGE_URL}/{relative})]({IMAGE_URL}/{relative})'}]))
    state = {'metadata': {'private': False, 'visibility': 'public', 'default_branch': 'main'},
             'parent': None, 'tree': None, 'marker': images.identity('o/r'), 'writes': []}

    def api(path, payload=None, method=None):
        if payload is not None:
            state['writes'].append((path, payload, method))
            if '/git/trees' in path:
                sha = hashlib.sha1(json.dumps(payload).encode()).hexdigest()
                state['next_tree'] = sha
                return {'sha': sha}
            if '/git/blobs' in path:
                return {'sha': hashlib.sha1(base64.b64decode(payload['content'])).hexdigest()}
            if '/git/commits' in path:
                return {'sha': 'd'*40}
            if '/git/refs' in path:
                if state.get('conflict'):
                    raise RuntimeError('Reference update failed: non-fast-forward')
                state.update(parent=payload['sha'], tree=state['next_tree'])
                return {}
            pytest.fail(f'Unexpected mutation: {path}')
        if path == 'repos/o/r':
            return state['metadata']
        if '/git/matching-refs/' in path:
            return [{'ref': 'refs/heads/docx-previews', 'object': {'sha': state['parent']}}] if state['parent'] else []
        if '/git/commits/' in path:
            return {'tree': {'sha': state['tree']}}
        if '/git/trees/' in path:
            return {'tree': [{'path': images.MARKER, 'mode': '100644', 'type': 'blob', 'sha': 'e'*40}]}
        if '/git/blobs/' in path:
            return {'encoding': 'base64', 'content': base64.b64encode(json.dumps(state['marker']).encode()).decode()}
        pytest.fail(f'Unexpected read: {path}')

    monkeypatch.setattr(images, 'api', api)
    return site, comments, picture, state


def test_branch_publishes_only_pngs_and_pins_comments_to_commit(host):
    site, comments, picture, state = host
    original = comments.read_text()
    commit = images.publish(site, comments, Options(image_host='branch'))
    body = json.loads(comments.read_text())[0]['body']
    assert IMAGE_URL not in body
    assert body.count(f'https://raw.githubusercontent.com/o/r/{commit}/') == 2
    assert 'github.io' not in body
    tree = next(payload for path, payload, _ in state['writes'] if path.endswith('/trees'))
    assert {entry['path'] for entry in tree['tree']} == {images.MARKER, 'README.md', str(picture.relative_to(site))}
    creation = next(payload for path, payload, _ in state['writes'] if path.endswith('/commits'))
    assert creation['parents'] == []
    # Rebuilding the same images reuses the commit rather than growing history.
    comments.write_text(original)
    state['writes'].clear()
    assert images.publish(site, comments, Options(image_host='branch')) == commit
    assert not any('/refs' in path or '/commits' in path for path, _, _ in state['writes'])


@pytest.mark.parametrize('invalid', ['private', 'default', 'unowned'])
def test_branch_checks_fail_before_writing_any_objects(host, invalid):
    site, comments, _, state = host
    options = Options(image_host='branch')
    if invalid == 'private':
        state['metadata'].update(private=True, visibility='private')
    elif invalid == 'default':
        options = Options(image_host='branch', preview_branch='main')
    else:
        state.update(parent='f'*40, tree='1'*40, marker=images.identity('someone/else'))
    with pytest.raises(ValueError):
        images.publish(site, comments, options)
    assert not state['writes']
    assert IMAGE_URL in comments.read_text()


def test_branch_update_is_fast_forward_and_failure_does_not_publish_links(host):
    site, comments, _, state = host
    state.update(parent='f'*40, tree='1'*40, conflict=True)
    with pytest.raises(RuntimeError, match='non-fast-forward'):
        images.publish(site, comments, Options(image_host='branch'))
    creation = next(payload for path, payload, _ in state['writes'] if path.endswith('/commits'))
    assert creation['parents'] == ['f'*40]
    assert state['writes'][-1] == ('repos/o/r/git/refs/heads/docx-previews', {'sha': 'd'*40, 'force': False}, 'PATCH')
    assert IMAGE_URL in comments.read_text()


@pytest.mark.parametrize('invalid', ['traversal', 'not-png', 'symlink'])
def test_invalid_image_payload_is_never_published(host, tmp_path, invalid):
    site, comments, picture, state = host
    if invalid == 'traversal':
        comments.write_text(comments.read_text().replace('/pr/1/', '/../pr/1/'))
    elif invalid == 'not-png':
        picture.write_bytes(b'<!doctype html>')
    else:
        other = tmp_path / 'outside.png'
        other.write_bytes(picture.read_bytes())
        picture.unlink()
        picture.symlink_to(other)
    with pytest.raises(ValueError):
        images.publish(site, comments, Options(image_host='branch'))
    assert not state['writes']


def test_no_images_means_no_branch_access(host, monkeypatch):
    site, comments, _, _ = host
    comments.write_text('[]')
    monkeypatch.setattr(images, 'api', lambda *args: pytest.fail('Nothing to publish'))
    assert images.publish(site, comments, Options(image_host='branch')) is None


def test_post_requires_successful_image_publication(host, monkeypatch):
    _, comments, _, _ = host
    monkeypatch.setattr(transport, 'api', lambda *args: {'state': 'open', 'head': {'sha': 'a'*40}})
    monkeypatch.setattr(transport, 'pages', lambda *args: [])
    with pytest.raises(ValueError, match='images must be published'):
        transport.post(comments)


def test_image_comment_budget_reserves_real_urls_and_keeps_change_logs():
    documents = [{'record': {'path': f'{i}/contract.docx', 'status': 'modified', 'redline': 'r.docx', 'revisions': 45},
        'url': None, 'images': [{'url': IMAGE_URL + f'/image-{i}.png', 'name': 'preview.png', 'anchor': 'change-1', 'kind': 'Text'}],
        'changes': [{'number': 1, 'kind': 'Text', 'id': 'change-1', 'markup': '<del>30</del><ins>45</ins>'}]} for i in range(3)]
    item = {'sha': 'a'*40, 'run_url': 'https://github.com/o/r/actions/runs/1'}
    body = review_comment(item, documents, options=Options(image_host='branch'))
    assert body.count('Enlarge excerpt 1') == 3
    assert body.count('Change log') == 3
    assert 'github.io' not in body and 'full document ↗' not in body
    body = review_comment(item, documents * 200, options=Options(image_host='branch'))
    assert len(body.replace(IMAGE_URL, 'x'*256).replace(ARTIFACT_URL, 'x'*256)) <= 58000
    assert 'of 600 documents here' in body


@pytest.mark.parametrize('branch', ['main..old', '../escape', 'refs//bad', 'test.lock', '.hidden', '-main', 'HEAD', 'x/'])
def test_invalid_branch_names_fail_validation(branch):
    with pytest.raises(ValueError, match='preview-branch'):
        Options(preview_branch=branch)


def test_host_options_are_independent_of_pages_and_comments():
    assert not Options().images_enabled
    assert Options(pages=True).images_enabled
    assert Options(image_host='branch').images_enabled
    assert not Options(pages=True, image_host='none').images_enabled
    assert not Options(image_host='branch', comments=False).images_enabled
    assert not Options(image_host='branch', inline_preview=False).images_enabled
    assert not Options(image_host='branch', preview_count=0).images_enabled
    assert Options(mode='latest', image_host='branch').images_enabled
    with pytest.raises(ValueError, match='image-host'):
        Options(image_host='invalid')
