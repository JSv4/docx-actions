"""Tests for the GitHub Action driver script (action/redline_changed.py).

Most tests exercise the pure/git-only helpers and need no engine binaries;
the two integration tests at the bottom run the real DocxodusEngine against
the repo fixtures, matching the requirements of the other test modules.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / 'action'))

import redline_changed as ra  # noqa: E402

FIXTURES = REPO_ROOT / 'tests' / 'fixtures'


def git(repo, *args, check=True):
    return subprocess.run(['git', '-C', str(repo)] + list(args), check=check,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / 'repo'
    path.mkdir()
    git(path, 'init', '-q')
    git(path, 'config', 'user.email', 'test@example.com')
    git(path, 'config', 'user.name', 'Test')
    return path


def commit_file(repo, rel, data: bytes, message='commit'):
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    git(repo, 'add', '-A')
    git(repo, 'commit', '-q', '-m', message)
    return git(repo, 'rev-parse', 'HEAD')


# ---------------------------------------------------------------------------
# input parsing / validation
# ---------------------------------------------------------------------------

def test_inputs_defaults():
    inputs = ra.Inputs.from_env({})
    assert inputs.engine == 'docxodus'
    assert inputs.files == '**/*.docx'
    assert inputs.output_dir == 'redlines'
    assert inputs.html_preview == 'auto'
    assert inputs.write_summary is True
    assert inputs.engine_kwargs() == {}


def test_inputs_engine_kwargs():
    inputs = ra.Inputs.from_env({'INPUT_DETECT_MOVES': 'true'})
    assert inputs.engine_kwargs() == {'detect_moves': True}


@pytest.mark.parametrize('value', ['wmlcomparer', 'docxdiff'])
def test_comparison_input_is_rejected(value):
    """Docxodus v11.0.0 deleted the engine selector the input mapped onto.

    Accepting it would either crash in the CLI or, worse, quietly produce
    DocxDiff output for a workflow that asked for WmlComparer.
    """
    with pytest.raises(ra.ConfigError, match='comparison'):
        ra.Inputs.from_env({'INPUT_COMPARISON': value})


def test_comparison_rejection_explains_the_removal():
    with pytest.raises(ra.ConfigError) as excinfo:
        ra.Inputs.from_env({'INPUT_COMPARISON': 'wmlcomparer'})

    message = str(excinfo.value)
    assert 'v11.0.0' in message
    assert 'DocxDiff' in message


# ---------------------------------------------------------------------------
# HTML preview mode resolution
#
# Docx2Html on NuGet now supports --track-changes, so the action self-test
# requires a rendered preview. That leaves the tolerant 'auto' path — the
# default, and the one most callers hit — without integration coverage, so its
# behaviour is pinned here instead, without depending on what is installed.
# ---------------------------------------------------------------------------

def test_resolve_previewer_returns_none_when_disabled(monkeypatch):
    """'false' must not even look for the tool — that is what makes .NET optional."""
    def fail():
        raise AssertionError('find_docx2html() called despite html-preview: false')

    monkeypatch.setattr(ra, 'find_docx2html', fail)
    inputs = ra.Inputs.from_env({'INPUT_HTML_PREVIEW': 'false'})
    assert ra.resolve_previewer(inputs) is None


def test_resolve_previewer_auto_warns_and_skips_when_tool_is_missing(monkeypatch, capsys):
    monkeypatch.setattr(ra, 'find_docx2html', lambda: None)
    inputs = ra.Inputs.from_env({'INPUT_HTML_PREVIEW': 'auto'})

    assert ra.resolve_previewer(inputs) is None
    assert '::warning::' in capsys.readouterr().out


def test_resolve_previewer_true_fails_when_tool_is_missing(monkeypatch):
    monkeypatch.setattr(ra, 'find_docx2html', lambda: None)
    inputs = ra.Inputs.from_env({'INPUT_HTML_PREVIEW': 'true'})

    with pytest.raises(ra.ConfigError, match='Docx2Html'):
        ra.resolve_previewer(inputs)


@pytest.mark.parametrize('mode', ['auto', 'true'])
def test_resolve_previewer_returns_the_tool_when_available(monkeypatch, mode):
    monkeypatch.setattr(ra, 'find_docx2html', lambda: '/usr/local/bin/docx2html')
    inputs = ra.Inputs.from_env({'INPUT_HTML_PREVIEW': mode})

    assert ra.resolve_previewer(inputs) == '/usr/local/bin/docx2html'


@pytest.mark.parametrize('env', [
    {'INPUT_ENGINE': 'wordperfect'},
    {'INPUT_HTML_PREVIEW': 'maybe'},
    {'INPUT_ORIGINAL': 'a.docx'},                              # original without modified
    {'INPUT_ENGINE': 'xmlpowertools', 'INPUT_DETECT_MOVES': 'true'},
    {'INPUT_DETECT_MOVES': 'yes'},                             # not a bool
])
def test_inputs_rejects_bad_combinations(env):
    with pytest.raises(ra.ConfigError):
        ra.Inputs.from_env(env)


# ---------------------------------------------------------------------------
# small pure helpers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('stdout,expected', [
    ('Revisions found: 9', 9),
    ('Redline complete: 11 revision(s) found', 11),
    ('nothing to see here', None),
    (None, None),
])
def test_revision_count_from_stdout(stdout, expected):
    assert ra.revision_count_from_stdout(stdout) == expected


def test_parse_name_status_handles_modifications_and_renames():
    raw = b'M\0docs/contract.docx\0R097\0old name.docx\0new name.docx\0A\0added.docx\0D\0gone.docx\0'
    changes = ra.parse_name_status(raw)
    assert [(c.status, c.path, c.previous_path) for c in changes] == [
        ('modified', 'docs/contract.docx', None),
        ('renamed', 'new name.docx', 'old name.docx'),
        ('added', 'added.docx', None),
        ('deleted', 'gone.docx', None),
    ]


def test_redline_output_paths_mirror_source_tree():
    docx, html = ra.redline_output_paths('redlines', 'docs/deals/Contract.DOCX')
    assert docx.as_posix() == 'redlines/docs/deals/Contract.redline.docx'
    assert html.as_posix() == 'redlines/docs/deals/Contract.redline.html'


def test_redline_output_paths_absolute_source_stays_under_output_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    inside = tmp_path / 'docs' / 'a.docx'
    docx, _ = ra.redline_output_paths('out', str(inside))
    assert docx.as_posix() == 'out/docs/a.redline.docx'
    outside = '/somewhere/else/entirely/b.docx'
    docx, _ = ra.redline_output_paths('out', outside)
    assert docx.as_posix() == 'out/b.redline.docx'
    traversal = '../outside/c.docx'
    docx, _ = ra.redline_output_paths('out', traversal)
    assert docx.as_posix() == 'out/c.redline.docx'


def test_build_summary_lists_each_change():
    changes = [
        ra.Change(path='a.docx', status='modified', revisions=3,
                  redline='redlines/a.redline.docx'),
        ra.Change(path='b.docx', status='added'),
        ra.Change(path='c.docx', status='modified', error='boom'),
    ]
    summary = ra.build_summary(changes, 'a' * 40, 'b' * 40)
    assert '| `a.docx` | Modified | 3 | `redlines/a.redline.docx` | — |' in summary
    assert 'Added (no base version to compare)' in summary
    assert '⚠️ failed' in summary


def test_build_summary_no_changes():
    summary = ra.build_summary([], None, None)
    assert 'No changed `.docx` files' in summary


def test_write_outputs(tmp_path):
    out = tmp_path / 'out.txt'
    changes = [ra.Change(path='a.docx', status='modified', revisions=2,
                         redline='redlines/a.redline.docx')]
    ra.write_outputs(changes, {'GITHUB_OUTPUT': str(out)})
    text = out.read_text()
    assert 'count=1\n' in text
    assert 'any-changes=true\n' in text
    payload = [line for line in text.splitlines() if line.startswith('redlines=')][0]
    parsed = json.loads(payload[len('redlines='):])
    assert parsed[0]['path'] == 'a.docx'
    assert parsed[0]['revisions'] == 2


# ---------------------------------------------------------------------------
# git plumbing against a real temporary repository
# ---------------------------------------------------------------------------

def test_detect_changes_and_read_blob(repo):
    base = commit_file(repo, 'docs/contract.docx', b'original bytes')
    (repo / 'notes.txt').write_text('irrelevant')
    head = commit_file(repo, 'docs/contract.docx', b'modified bytes')

    changes = ra.detect_changes(base, head, ['**/*.docx'], cwd=str(repo))
    assert [(c.status, c.path) for c in changes] == [('modified', 'docs/contract.docx')]
    assert ra.read_blob(base, 'docs/contract.docx', cwd=str(repo)) == b'original bytes'
    assert ra.read_blob(head, 'docs/contract.docx', cwd=str(repo)) == b'modified bytes'


def test_detect_changes_pure_rename(repo):
    base = commit_file(repo, 'a.docx', b'same bytes either way')
    git(repo, 'mv', 'a.docx', 'b.docx')
    git(repo, 'commit', '-q', '-m', 'rename')
    head = git(repo, 'rev-parse', 'HEAD')

    changes = ra.detect_changes(base, head, ['**/*.docx'], cwd=str(repo))
    assert [(c.status, c.path, c.previous_path) for c in changes] == [
        ('renamed', 'b.docx', 'a.docx')]


def test_detect_changes_respects_patterns_and_extension(repo):
    base = commit_file(repo, 'docs/in-scope.docx', b'v1')
    (repo / 'other.docx').write_bytes(b'v1')
    (repo / 'docs' / 'readme.txt').write_text('v1')
    git(repo, 'add', '-A')
    git(repo, 'commit', '-q', '-m', 'more files')
    (repo / 'docs' / 'in-scope.docx').write_bytes(b'v2')
    (repo / 'other.docx').write_bytes(b'v2')
    (repo / 'docs' / 'readme.txt').write_text('v2')
    git(repo, 'add', '-A')
    head = commit_file(repo, 'docs/in-scope.docx', b'v3')

    changes = ra.detect_changes(base, head, ['docs/*.docx'], cwd=str(repo))
    assert [c.path for c in changes] == ['docs/in-scope.docx']


def test_resolve_refs_push_event(repo):
    base = commit_file(repo, 'a.docx', b'v1')
    head = commit_file(repo, 'a.docx', b'v2')
    inputs = ra.Inputs.from_env({})
    resolved = ra.resolve_refs(
        inputs, {'GITHUB_EVENT_NAME': 'push'},
        {'before': base, 'after': head}, cwd=str(repo))
    assert resolved == (base, head)


def test_resolve_refs_push_event_new_branch_uses_parent(repo):
    base = commit_file(repo, 'a.docx', b'v1')
    head = commit_file(repo, 'a.docx', b'v2')
    inputs = ra.Inputs.from_env({})
    resolved = ra.resolve_refs(
        inputs, {'GITHUB_EVENT_NAME': 'push'},
        {'before': '0' * 40, 'after': head}, cwd=str(repo))
    assert resolved == (base, head)


def test_resolve_refs_pull_request_uses_merge_base(repo):
    fork_point = commit_file(repo, 'a.docx', b'v1')
    default_branch = git(repo, 'rev-parse', '--abbrev-ref', 'HEAD')
    git(repo, 'checkout', '-q', '-b', 'feature')
    pr_head = commit_file(repo, 'a.docx', b'feature edit')
    git(repo, 'checkout', '-q', default_branch)
    base_tip = commit_file(repo, 'unrelated.txt', b'base moved on')

    inputs = ra.Inputs.from_env({})
    event = {'pull_request': {'base': {'sha': base_tip}, 'head': {'sha': pr_head}}}
    resolved = ra.resolve_refs(
        inputs, {'GITHUB_EVENT_NAME': 'pull_request'}, event, cwd=str(repo))
    assert resolved == (fork_point, pr_head)


def test_resolve_refs_explicit_inputs_win(repo):
    base = commit_file(repo, 'a.docx', b'v1')
    head = commit_file(repo, 'a.docx', b'v2')
    inputs = ra.Inputs.from_env({'INPUT_BASE_REF': 'HEAD~1', 'INPUT_HEAD_REF': 'HEAD'})
    resolved = ra.resolve_refs(inputs, {'GITHUB_EVENT_NAME': 'push'}, {}, cwd=str(repo))
    assert resolved == (base, head)


def test_resolve_refs_unresolvable_ref_raises(repo):
    commit_file(repo, 'a.docx', b'v1')
    inputs = ra.Inputs.from_env({'INPUT_BASE_REF': 'no-such-ref', 'INPUT_HEAD_REF': 'HEAD'})
    with pytest.raises(ra.ConfigError):
        ra.resolve_refs(inputs, {}, {}, cwd=str(repo))


# ---------------------------------------------------------------------------
# integration: run main() with the real engine over the repo fixtures
# ---------------------------------------------------------------------------

def test_main_explicit_pair(tmp_path, monkeypatch):
    monkeypatch.chdir(REPO_ROOT)
    out_file, summary_file = tmp_path / 'out.txt', tmp_path / 'summary.md'
    env = {
        'INPUT_ORIGINAL': str(FIXTURES / 'original.docx'),
        'INPUT_MODIFIED': str(FIXTURES / 'modified.docx'),
        'INPUT_OUTPUT_DIR': str(tmp_path / 'redlines'),
        'INPUT_HTML_PREVIEW': 'false',
        'GITHUB_OUTPUT': str(out_file),
        'GITHUB_STEP_SUMMARY': str(summary_file),
    }
    assert ra.main(env) == 0

    text = out_file.read_text()
    assert 'count=1\n' in text
    payload = [line for line in text.splitlines() if line.startswith('redlines=')][0]
    record = json.loads(payload[len('redlines='):])[0]
    assert record['revisions'] == 10
    redline = Path(record['redline'])
    assert redline.is_file() and redline.stat().st_size > 0
    # absolute source paths must not escape the requested output directory
    assert (tmp_path / 'redlines') in redline.resolve().parents
    assert 'DOCX redlines' in summary_file.read_text()


def test_main_auto_detect_over_git_history(repo, tmp_path, monkeypatch):
    base = commit_file(repo, 'contracts/agreement.docx',
                       (FIXTURES / 'original.docx').read_bytes())
    head = commit_file(repo, 'contracts/agreement.docx',
                       (FIXTURES / 'modified.docx').read_bytes())
    monkeypatch.chdir(repo)

    out_file, summary_file = tmp_path / 'out.txt', tmp_path / 'summary.md'
    env = {
        'INPUT_BASE_REF': base,
        'INPUT_HEAD_REF': head,
        'INPUT_OUTPUT_DIR': str(tmp_path / 'redlines'),
        'INPUT_HTML_PREVIEW': 'false',
        'GITHUB_OUTPUT': str(out_file),
        'GITHUB_STEP_SUMMARY': str(summary_file),
    }
    assert ra.main(env) == 0

    payload = [line for line in out_file.read_text().splitlines()
               if line.startswith('redlines=')][0]
    record = json.loads(payload[len('redlines='):])[0]
    assert record['path'] == 'contracts/agreement.docx'
    assert record['status'] == 'modified'
    assert record['revisions'] == 10
    assert Path(record['redline']).is_file()
    assert 'contracts/agreement.docx' in summary_file.read_text()


def test_all_files_root_nested_uppercase_and_one_sided_views(repo, tmp_path, monkeypatch):
    original = (FIXTURES / 'original.docx').read_bytes()
    modified = (FIXTURES / 'modified.docx').read_bytes()
    for path in ('Root.DOCX', 'one/agreement.docx', 'two/agreement.docx', 'deleted.docx', 'old.docx'):
        # Keep the rename's blob unique so Git cannot equally attribute it to
        # the separately deleted file. ZIP readers permit trailing bytes.
        commit_file(repo, path, original + b'unique rename fixture' if path == 'old.docx' else original)
    base = git(repo, 'rev-parse', 'HEAD')
    for path in ('Root.DOCX', 'one/agreement.docx', 'two/agreement.docx'):
        (repo / path).write_bytes(modified)
    (repo / 'deleted.docx').unlink()
    git(repo, 'mv', 'old.docx', 'renamed.docx')
    head = commit_file(repo, 'added.docx', modified)
    monkeypatch.chdir(repo)
    output = tmp_path / 'out'
    assert ra.main({'INPUT_BASE_REF': base, 'INPUT_HEAD_REF': head,
                    'INPUT_OUTPUT_DIR': str(output), 'INPUT_HTML_PREVIEW': 'false',
                    'INPUT_RENDER_UNPAIRED': 'true'}) == 0
    records = json.loads((output / 'manifest.json').read_text())['files']
    assert len(records) == 6
    by_path = {record['path']: record for record in records}
    for path in ('Root.DOCX', 'one/agreement.docx', 'two/agreement.docx'):
        assert by_path[path]['redline']
        assert (output / by_path[path]['redline']).is_file()
    assert by_path['one/agreement.docx']['redline'] != by_path['two/agreement.docx']['redline']
    for path in ('added.docx', 'deleted.docx', 'renamed.docx'):
        assert by_path[path]['document']
        assert by_path[path]['redline'] is None


def test_required_html_failure_is_reported_but_other_files_continue(repo, tmp_path, monkeypatch):
    original = (FIXTURES / 'original.docx').read_bytes()
    modified = (FIXTURES / 'modified.docx').read_bytes()
    commit_file(repo, 'a.docx', original)
    base = commit_file(repo, 'b.docx', original)
    (repo / 'a.docx').write_bytes(modified)
    head = commit_file(repo, 'b.docx', modified)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(ra, 'resolve_previewer', lambda inputs: 'test-tool')
    monkeypatch.setattr(ra, 'generate_preview', lambda *args: False)
    output = tmp_path / 'out'
    assert ra.main({'INPUT_BASE_REF': base, 'INPUT_HEAD_REF': head,
                    'INPUT_OUTPUT_DIR': str(output), 'INPUT_HTML_PREVIEW': 'true'}) == 1
    records = json.loads((output / 'manifest.json').read_text())['files']
    assert len(records) == 2
    assert all(record['error'] and record['redline'] for record in records)


@pytest.mark.parametrize('mode', ['latest', 'both'])
def test_modes_preserve_the_head_document_and_latest_skips_comparison(repo, tmp_path, monkeypatch, mode):
    original = (FIXTURES / 'original.docx').read_bytes()
    modified = (FIXTURES / 'modified.docx').read_bytes()
    commit_file(repo, 'removed.docx', original)
    base = commit_file(repo, 'nested/contract.DOCX', original)
    (repo / 'removed.docx').unlink()
    head = commit_file(repo, 'nested/contract.DOCX', modified)
    monkeypatch.chdir(repo)
    if mode == 'latest':
        monkeypatch.setattr(ra, 'make_engine', lambda *_: pytest.fail('Latest mode must not initialize the differ'))
    output = tmp_path / mode
    assert ra.main({'INPUT_BASE_REF': base, 'INPUT_HEAD_REF': head, 'INPUT_MODE': mode,
                    'INPUT_OUTPUT_DIR': str(output), 'INPUT_HTML_PREVIEW': 'false',
                    'INPUT_RENDER_UNPAIRED': 'true'}) == 0
    manifest = json.loads((output / 'manifest.json').read_text())
    assert manifest['mode'] == mode
    records = {r['path']: r for r in manifest['files']}
    current = records['nested/contract.DOCX']
    assert (output / current['latest']).read_bytes() == modified
    assert records['removed.docx']['latest'] is None
    if mode == 'latest':
        assert all(r['redline'] is None and r['revisions'] is None for r in records.values())
        assert records['removed.docx']['document'] is None
    else:
        assert current['revisions'] == 10 and current['redline'] != current['latest']
        assert records['removed.docx']['document']


@pytest.mark.parametrize('with_original', [True, False])
def test_latest_mode_supports_explicit_pair_without_comparison(tmp_path, monkeypatch, with_original):
    monkeypatch.setattr(ra, 'make_engine', lambda *_: pytest.fail('No differ expected'))
    assert ra.main({'INPUT_ORIGINAL': str(FIXTURES / 'original.docx') if with_original else '',
                    'INPUT_MODIFIED': str(FIXTURES / 'modified.docx'), 'INPUT_MODE': 'latest',
                    'INPUT_HTML_PREVIEW': 'false', 'INPUT_OUTPUT_DIR': str(tmp_path)}) == 0
    [record] = json.loads((tmp_path / 'manifest.json').read_text())['files']
    assert (tmp_path / record['latest']).read_bytes() == (FIXTURES / 'modified.docx').read_bytes()
    assert record['redline'] is None
