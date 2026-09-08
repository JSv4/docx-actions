"""Host comment PNGs on a dedicated public-repository branch without Pages."""

import argparse
import base64
import json
import os
from pathlib import Path
import re
from urllib.parse import quote

from github import api
from options import IMAGE_URL, Options

MARKER = 'docx-actions-images.json'
IMAGE_PATH = re.compile(r'pr/[1-9][0-9]*/[0-9a-f]{40}/file-[0-9a-f]{20}/preview-[1-9][0-9]*-[0-9a-f]{12}\.png')


def identity(repo):
    return {'generator': 'JSv4/docx-actions', 'repository': repo, 'purpose': 'comment-images', 'schema_version': 1}


def check_branch(options):
    repo = os.environ['GITHUB_REPOSITORY']
    metadata = api(f'repos/{repo}')
    if metadata.get('visibility') != 'public' or metadata.get('private', True):
        raise ValueError('image-host: branch requires a public repository so GitHub can load the images. '
                         'Use image-host: auto for text comments or an explicitly enabled Pages viewer.')
    if options.preview_branch == metadata['default_branch']:
        raise ValueError('preview-branch must be a dedicated branch, never the default branch')
    ref = f'heads/{options.preview_branch}'
    matches = api(f'repos/{repo}/git/matching-refs/{quote(ref, safe="/")}')
    parent = next((entry['object']['sha'] for entry in matches if entry['ref'] == f'refs/{ref}'), None)
    tree_sha = None
    if parent:
        tree_sha = api(f'repos/{repo}/git/commits/{parent}')['tree']['sha']
        tree = api(f'repos/{repo}/git/trees/{tree_sha}')
        marker = next((entry for entry in tree['tree'] if entry['path'] == MARKER
                       and entry['type'] == 'blob' and entry['mode'] == '100644'), None)
        value = None
        if marker:
            blob = api(f'repos/{repo}/git/blobs/{marker["sha"]}')
            if blob.get('encoding') == 'base64':
                try:
                    value = json.loads(base64.b64decode(blob['content']))
                except (ValueError, UnicodeError):
                    pass
        if tree.get('truncated') or value != identity(repo):
            raise ValueError(f'Branch {options.preview_branch!r} is not owned by DOCX Actions. '
                             'Choose an unused preview-branch; no branch was changed.')
    return repo, parent, tree_sha


def publish(output, comments_path, options):
    previews = json.loads(comments_path.read_text())
    # Only files actually referenced by current comments are published. HTML,
    # Word files, and source filenames never enter the image branch.
    paths = sorted({path for preview in previews for path in
                    re.findall(re.escape(IMAGE_URL) + r'/([^\s)]+)', preview['body'])})
    if not paths:
        print('No comment images to publish.')
        return None
    repo, parent, old_tree = check_branch(options)
    root = output.resolve()
    files = []
    for relative in paths:
        target = root / relative
        if not IMAGE_PATH.fullmatch(relative) or not target.resolve().is_relative_to(root):
            raise ValueError('Invalid preview image path')
        content = target.read_bytes()
        if not content.startswith(b'\x89PNG\r\n\x1a\n') or len(content) > 10 * 1024 * 1024:
            raise ValueError('Preview images must be PNG files no larger than 10 MiB')
        files.append((relative, content))
    # Validate every file before creating any Git objects.
    entries = [{'path': MARKER, 'mode': '100644', 'type': 'blob',
                'content': json.dumps(identity(repo), sort_keys=True) + '\n'},
               {'path': 'README.md', 'mode': '100644', 'type': 'blob', 'content':
                '# DOCX review images\n\nGenerated PNG excerpts for pull request comments. '
                'This branch does not deploy GitHub Pages. Do not merge it into your source branch.\n\n'
                'Comment URLs are pinned to commits. Earlier images remain in Git history; '
                'Actions artifact retention does not remove them.\n'}]
    for relative, content in files:
        blob = api(f'repos/{repo}/git/blobs', {'content': base64.b64encode(content).decode('ascii'), 'encoding': 'base64'})
        entries.append({'path': relative, 'mode': '100644', 'type': 'blob', 'sha': blob['sha']})
    tree = api(f'repos/{repo}/git/trees', {'tree': entries})['sha']
    commit = parent
    if tree != old_tree:
        commit = api(f'repos/{repo}/git/commits', {'message': 'Update DOCX review images',
                     'tree': tree, 'parents': [parent] if parent else []})['sha']
        if parent:
            # A concurrent writer causes a non-fast-forward error; never force.
            api(f'repos/{repo}/git/refs/heads/{quote(options.preview_branch, safe="/")}',
                {'sha': commit, 'force': False}, method='PATCH')
        else:
            api(f'repos/{repo}/git/refs', {'ref': f'refs/heads/{options.preview_branch}', 'sha': commit})
    base_url = f'https://raw.githubusercontent.com/{repo}/{commit}'
    if len(base_url) > 256 or not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('Invalid published image URL')
    for preview in previews:
        preview['body'] = preview['body'].replace(IMAGE_URL, base_url)
    comments_path.write_text(json.dumps(previews, indent=2) + '\n', encoding='utf-8')
    print(f'Published {len(files)} comment images to {options.preview_branch} at {commit[:12]}.')
    return commit


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['check', 'publish'])
    parser.add_argument('--output', type=Path, default=Path('_site'))
    parser.add_argument('--comments', type=Path, default=Path('_preview_comments.json'))
    args = parser.parse_args()
    options = Options.from_env()
    if options.image_host != 'branch':
        parser.error('This command requires image-host: branch')
    if args.command == 'check':
        check_branch(options)
    else:
        publish(args.output, args.comments, options)
