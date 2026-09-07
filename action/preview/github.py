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
import zipfile


def api(path, payload=None):
    command = ['gh', 'api', path]
    if payload is not None:
        command += ['--method', 'PATCH' if '/issues/comments/' in path else 'POST', '--input', '-']
    return json.loads(subprocess.check_output(command, input=json.dumps(payload) if payload is not None else None, text=True))


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
        for field in ('redline', 'html', 'document'):
            value = record.get(field)
            if value:
                path = PurePosixPath(value)
                if path.is_absolute() or '..' in path.parts or '\\' in value:
                    raise ValueError('Invalid result path')


def collect(destination):
    repo = os.environ['GITHUB_REPOSITORY']
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
            catalog.append({'pr': number, 'title': pr['title'], 'sha': manifest['head_sha'],
                            'current_head': pr['head']['sha'], 'pr_url': pr['html_url'],
                            'run_url': run['html_url'], 'input_dir': str(source)})
            break
    (destination / 'catalog.json').write_text(json.dumps(catalog, indent=2) + '\n', encoding='utf-8')
    print(f'Collected {len(catalog)} pull request comparisons')


MARKER = re.compile(r'^<!-- docx-redlines-preview:(index|[0-9a-f]{20}) -->')


def post(comments_path, base_url):
    repo = os.environ['GITHUB_REPOSITORY']
    for preview in json.loads(comments_path.read_text()):
        number = int(preview['pr'])
        pr = api(f'repos/{repo}/pulls/{number}')
        if pr['state'] != 'open' or pr['head']['sha'] != preview['sha']:
            continue
        existing = {}
        for comment in pages(f'repos/{repo}/issues/{number}/comments?per_page=100'):
            match = MARKER.match(comment.get('body') or '')
            if match and comment['user']['login'] == 'github-actions[bot]':
                existing[match[1]] = comment
            elif (comment.get('body') or '').startswith('<!-- docxodus-inline-preview -->') and comment['user']['login'] == 'github-actions[bot]':
                # Upgrade the original standalone demo's summary in place.
                existing.setdefault('index', comment)
        # Avoid commenting on every code-only PR. Update an existing review
        # when its final DOCX change disappears, so it cannot look current.
        if len(preview['comments']) == 1 and not existing:
            continue
        active_keys = set()
        for entry in preview['comments']:
            key, body = entry['key'], entry['body']
            if len(body) > 60000 or not body.startswith(f'<!-- docx-redlines-preview:{key} -->'):
                raise ValueError('Invalid generated comment')
            active_keys.add(key)
            old = existing.get(key)
            if old and old['body'] == body:
                continue
            # Recheck before each write; a newer push must not acquire a stale
            # preview when a large PR requires several API requests.
            current = api(f'repos/{repo}/pulls/{number}')
            if current['state'] != 'open' or current['head']['sha'] != preview['sha']:
                break
            endpoint = f"repos/{repo}/issues/comments/{old['id']}" if old else f'repos/{repo}/issues/{number}/comments'
            api(endpoint, {'body': body})
        else:
            for key, old in existing.items():
                if key not in active_keys:
                    current = api(f'repos/{repo}/pulls/{number}')
                    if current['state'] != 'open' or current['head']['sha'] != preview['sha']:
                        break
                    body = f"<!-- docx-redlines-preview:{key} -->\nThis document is no longer changed in this pull request at commit `{preview['sha'][:12]}`.\n"
                    if old['body'] != body:
                        api(f"repos/{repo}/issues/comments/{old['id']}", {'body': body})
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as handle:
            handle.write(f'\n## Word document review\n\n[Open the browser viewer]({base_url})\n')


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
    publish.add_argument('--base-url', required=True)
    args = parser.parse_args()
    if args.command == 'context':
        context(args.pr, args.output)
    elif args.command == 'collect':
        collect(args.output)
    else:
        post(args.comments, args.base_url)
