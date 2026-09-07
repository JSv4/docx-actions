"""GitHub transport for the reusable review workflow. Artifacts are data only."""

import argparse
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
from urllib.parse import quote
from urllib.error import HTTPError, URLError
from urllib.request import urlopen
import zipfile
from options import Options


def api(path, payload=None, method=None):
    command = ['gh', 'api', path]
    if method:
        command += ['--method', method]
    if payload is not None:
        if not method:
            command += ['--method', 'PATCH' if '/issues/comments/' in path else 'POST']
        command += ['--input', '-']
    result = subprocess.check_output(command, input=json.dumps(payload) if payload is not None else None, text=True)
    return json.loads(result) if result.strip() else None


def pages(path, key=None):
    result = json.loads(subprocess.check_output(['gh', 'api', '--paginate', '--slurp', path], text=True))
    for page in result:
        yield from page[key] if key else page


def write_env(name, value, target='GITHUB_OUTPUT'):
    if '\n' in str(value) or '\r' in str(value):
        raise ValueError('Environment values must be single-line')
    with open(os.environ[target], 'a', encoding='utf-8') as handle:
        handle.write(f'{name}={value}\n')


def context(pr_number, destination):
    event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text())
    pr_number = int(event.get('pull_request', {}).get('number') or pr_number)
    if pr_number < 1:
        raise ValueError('A pull request number is required for comparison')
    pr = api(f"repos/{os.environ['GITHUB_REPOSITORY']}/pulls/{pr_number}")
    destination.mkdir(parents=True, exist_ok=True)
    event_path = (destination / 'event.json').resolve()
    event_path.write_text(json.dumps({'pull_request': pr}), encoding='utf-8')
    write_env('pr', pr_number)
    write_env('base', pr['base']['sha'])
    write_env('head', pr['head']['sha'])
    write_env('open', str(pr['state'] == 'open').lower())
    write_env('DOCX_EVENT_PATH', event_path, 'GITHUB_ENV')
    write_env('DOCX_EVENT_NAME', 'pull_request_target', 'GITHUB_ENV')


def extract_artifact(data, destination):
    """Reject traversal, symlinks, executable payloads, and oversized ZIPs."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if sum(entry.file_size for entry in archive.infolist()) > 500 * 1024 * 1024:
            raise ValueError('Comparison artifact exceeds 500 MiB uncompressed')
        for entry in archive.infolist():
            path = PurePosixPath(entry.filename)
            if path.is_absolute() or '..' in path.parts or '\\' in entry.filename:
                raise ValueError('Artifact contains an unsafe path')
            if stat.S_ISLNK(entry.external_attr >> 16):
                raise ValueError('Artifact contains a symbolic link')
            if entry.is_dir():
                continue
            if path.suffix.lower() not in ('.json', '.html', '.docx'):
                raise ValueError('Artifact contains an unexpected file type')
            target = destination / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(entry))


def validate_manifest(manifest, repo, pr_number):
    if manifest.get('schema_version') != 1 or manifest.get('repository', '').lower() != repo.lower() or manifest.get('pr') != pr_number:
        raise ValueError('Artifact provenance does not match the pull request')
    for field in ('base_sha', 'head_sha'):
        if not re.fullmatch(r'[0-9a-f]{40}', manifest.get(field) or ''):
            raise ValueError('Invalid comparison commit')
    if not isinstance(manifest.get('files'), list):
        raise ValueError('Missing file results')
    paths = set()
    for record in manifest['files']:
        if not isinstance(record.get('path'), str) or not record['path'].lower().endswith('.docx') or record['path'] in paths:
            raise ValueError('Invalid or duplicate document path')
        paths.add(record['path'])
        if record.get('status') not in ('modified', 'renamed', 'added', 'deleted', 'explicit'):
            raise ValueError('Invalid document status')
        for field in ('redline', 'html', 'document', 'latest', 'latest_html'):
            value = record.get(field)
            if value:
                path = PurePosixPath(value)
                if path.is_absolute() or '..' in path.parts or '\\' in value:
                    raise ValueError('Invalid result path')


def collect(destination):
    repo = os.environ['GITHUB_REPOSITORY']
    mode = Options.from_env().mode
    current_run = api(f"repos/{repo}/actions/runs/{int(os.environ['GITHUB_RUN_ID'])}")
    pulls = list(pages(f'repos/{repo}/pulls?state=open&per_page=100'))
    catalog = []
    destination.mkdir(parents=True, exist_ok=True)
    for pr in pulls:
        number = int(pr['number'])
        name = f'docx-actions-pr-{number}'
        artifacts = pages(f'repos/{repo}/actions/artifacts?name={quote(name)}&per_page=100', 'artifacts')
        for artifact in artifacts:
            if artifact['expired'] or artifact['name'] != name:
                continue
            run_id = artifact['workflow_run']['id']
            run = api(f'repos/{repo}/actions/runs/{run_id}')
            # A similarly named artifact from PR-controlled workflows is not
            # accepted. Reuse only this caller's trusted publishing workflow.
            if run['workflow_id'] != current_run['workflow_id'] or run['event'] not in ('pull_request_target', 'workflow_dispatch'):
                continue
            if run_id != current_run['id'] and run['status'] != 'completed':
                continue
            source = destination / f'pr-{number}' / str(artifact['id'])
            data = subprocess.check_output(['gh', 'api', f"repos/{repo}/actions/artifacts/{artifact['id']}/zip"])
            extract_artifact(data, source)
            manifest = json.loads((source / 'manifest.json').read_text())
            validate_manifest(manifest, repo, number)
            if manifest.get('mode', 'redline') != mode:
                continue
            catalog.append({'pr': number, 'title': pr['title'], 'sha': manifest['head_sha'],
                            'current_head': pr['head']['sha'], 'pr_url': pr['html_url'],
                            'run_url': run['html_url'], 'input_dir': str(source)})
            break
    (destination / 'catalog.json').write_text(json.dumps(catalog, indent=2) + '\n', encoding='utf-8')
    print(f'Collected {len(catalog)} pull request comparisons')


MARKER = re.compile(r'^<!-- docx-redlines-preview:(index|[0-9a-f]{20}) -->')


def post(comments_path, base_url='', artifact_url=''):
    repo = os.environ['GITHUB_REPOSITORY']
    for preview in json.loads(comments_path.read_text()):
        number = int(preview['pr'])

        def current():
            pr = api(f'repos/{repo}/pulls/{number}')
            return pr['state'] == 'open' and pr['head']['sha'] == preview['sha']

        if not current():
            continue
        owned, summaries = [], []
        for comment in pages(f'repos/{repo}/issues/{number}/comments?per_page=100'):
            match = MARKER.match(comment.get('body') or '')
            legacy = (comment.get('body') or '').startswith('<!-- docxodus-inline-preview -->')
            if comment['user']['login'] == 'github-actions[bot]' and (match or legacy):
                owned.append(comment)
                if legacy or match[1] == 'index':
                    summaries.append(comment)
        # Avoid commenting on every code-only PR. Update an existing review
        # when its final DOCX change disappears, so it cannot look current.
        if preview['file_count'] == 0 and not owned:
            continue
        body = preview['body']
        placeholder = 'https://docx-actions.invalid/review-artifact'
        if placeholder in body:
            if not artifact_url.startswith(f'https://github.com/{repo}/actions/runs/'):
                raise ValueError('The uploaded review artifact URL is required before commenting')
            body = body.replace(placeholder, artifact_url)
        if len(body) > 60000 or not body.startswith('<!-- docx-redlines-preview:index -->'):
            raise ValueError('Invalid generated comment')
        # Keep the original summary URL, including when migrating from the
        # demo's first comment format. Remove only our obsolete bot comments,
        # and only after the complete replacement exists successfully.
        summary = min(summaries, key=lambda c: c['id']) if summaries else None
        if not summary or summary['body'] != body:
            if not current():
                continue
            endpoint = f"repos/{repo}/issues/comments/{summary['id']}" if summary else f'repos/{repo}/issues/{number}/comments'
            result = api(endpoint, {'body': body})
            if summary is None:
                summary = result
        for comment in owned:
            if comment['id'] == summary['id']:
                continue
            if not current():
                break
            api(f"repos/{repo}/issues/comments/{comment['id']}", method='DELETE')
    if os.environ.get('GITHUB_STEP_SUMMARY') and Options.from_env().summary:
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as handle:
            handle.write('\n## Word document review\n\n')
            if base_url:
                handle.write(f'[Open the browser viewer]({base_url})\n\n')
            if artifact_url:
                handle.write(f'[Download the complete review]({artifact_url})\n')


def read_site(url):
    try:
        with urlopen(url, timeout=30) as response:
            return response.read(1_000_000).decode('utf-8')
    except HTTPError as error:
        if error.code == 404:
            return None
        raise RuntimeError('Cannot verify ownership of the existing Pages site') from error
    except (URLError, UnicodeError) as error:
        raise RuntimeError('Cannot verify ownership of the existing Pages site') from error


def check_pages(allow_overwrite=False):
    """Only publish over an empty site or an identifiable DOCX Actions site."""
    repo = os.environ['GITHUB_REPOSITORY']
    site = api(f'repos/{repo}/pages')
    if allow_overwrite:
        print('Explicitly authorized replacement of this repository\'s Pages site.')
        return
    url = site['html_url'].rstrip('/')
    if not url.startswith('https://'):
        raise ValueError('Pages must use HTTPS before its ownership can be verified')
    marker = read_site(url + '/docx-actions-site.json')
    if marker:
        try:
            identity = json.loads(marker)
        except ValueError:
            identity = {}
        if isinstance(identity, dict) and identity.get('generator') == 'JSv4/docx-actions' and identity.get('repository', '').lower() == repo.lower():
            return
    landing = read_site(url + '/')
    # The first release predates the ownership marker. Recognize its exact
    # generated landing page so existing consumers can opt in without an override.
    if landing and all(part in landing for part in (
        '<title>Word document previews · DOCX review</title>',
        'Generated with Python-Redlines and Docxodus. Each comparison identifies its source commit.',
        'WORD DOCUMENT REVIEW · GITHUB ACTIONS',
    )):
        return
    deployments = api(f'repos/{repo}/deployments?environment=github-pages&per_page=1')
    if landing is None and marker is None and not deployments and site.get('status') != 'built':
        return
    raise RuntimeError('Pages already contains a site that DOCX Actions does not own. '
                       'No deployment was made. Keep pages: false, or explicitly set '
                       'allow-pages-overwrite: true to replace the entire site.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    ctx = commands.add_parser('context')
    ctx.add_argument('--pr', type=int, default=0)
    ctx.add_argument('--output', type=Path, required=True)
    gather = commands.add_parser('collect')
    gather.add_argument('--output', type=Path, default=Path('_preview_inputs'))
    publish = commands.add_parser('post')
    publish.add_argument('--comments', type=Path, default=Path('_preview_comments.json'))
    publish.add_argument('--base-url', default='')
    publish.add_argument('--artifact-url', default='')
    guard = commands.add_parser('check-pages')
    guard.add_argument('--allow-overwrite', action='store_true')
    args = parser.parse_args()
    if args.command == 'context':
        context(args.pr, args.output)
    elif args.command == 'collect':
        collect(args.output)
    elif args.command == 'post':
        post(args.comments, args.base_url, args.artifact_url)
    else:
        check_pages(args.allow_overwrite)
