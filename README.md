# DOCX Actions

Automatically review **every changed Word document** in a GitHub pull request.
Get contextual previews in the PR, a complete browser viewer, and downloadable
Word files with native tracked changes. Powered by
[Docxodus](https://github.com/JSv4/Docxodus) through
[Python-Redlines](https://github.com/JSv4/Python-Redlines).

## Install once

Enable **Settings → Pages → Source: GitHub Actions**, then add
`.github/workflows/docx-review.yml` to your default branch:

```yaml
name: Word document review
on:
  pull_request_target:
    types: [opened, synchronize, reopened, closed]
  workflow_dispatch:

permissions:
  contents: read
  actions: read
  pull-requests: write
  pages: write
  id-token: write

jobs:
  review:
    uses: JSv4/docx-actions/.github/workflows/review.yml@main
```

That's the complete consumer workflow. No document paths, version pairs,
Python scripts, browser setup, personal access token, or custom secrets are
needed. Pin `@main` to a reviewed commit SHA for reproducible installations.

The workflow owns the repository's Pages site. Use a dedicated repository if
you already publish a different website there. Pages visibility determines who
can read the documents; enabling this workflow publishes the complete documents
and downloadable Word files there. This installation targets GitHub.com and
GitHub-hosted Ubuntu runners.

## What happens on a pull request

- All changed `.docx` files are discovered, including root-level files, nested
  folders, uppercase extensions, and filenames containing spaces.
- Modified documents are compared against the PR's merge-base. Renames preserve
  their previous path; a pure rename is labeled as unchanged content.
- Added and deleted documents get full-document views, clearly labeled as
  one-sided snapshots rather than tracked-change comparisons.
- Each document gets its own updatable PR comment. Text edits use insertions and
  strikethroughs; two representative image excerpts include surrounding text and
  **Expand in full document** links. A large contract cannot hide the other files.
- The complete viewer has Previous/Next navigation, passage deep links, and a
  Word download. Long comments disclose truncation and link to every passage.
- Pushing another edit updates the bot's existing comments. Comparisons identify
  their source commit, and a newer PR head prevents stale comments from posting.
- Open PRs share one index; publishing one PR retains the others. Closing a PR
  removes it from the next site build. Code-only PRs don't get new preview comments.

Previews are rebuilt from retained comparison artifacts (90 days by default).
If an artifact expires, its preview disappears on the next rebuild. Manual runs
rebuild the index; to recompute a particular PR, pass the optional `pull-request`
input from a caller workflow. An unchanged PR does not automatically refresh an
expired artifact.

GitHub comments cannot embed the fully styled document. Inline images and a
small HTML subset provide the in-GitHub view; Pages hosts the full rendering.
See the complex NVCA contract in the separate
[demo repository](https://github.com/JSv4/docxodus-action-demo).

## Optional inputs

The reusable workflow accepts:

| Input | Default | Purpose |
|---|---|---|
| `files` | `**/*.docx` | Restrict discovery with newline-separated Git pathspec globs. |
| `pull-request` | Event PR number | Recompute a PR from a manual run. |
| `publish` | `true` | Set `false` to build an inspectable site artifact without deploying or commenting. |

For example, an optional restriction is simply:

```yaml
jobs:
  review:
    uses: JSv4/docx-actions/.github/workflows/review.yml@main
    with:
      files: 'contracts/**/*.docx'
```

## Comparison without publishing

The repository root also exposes a conventional composite action. It discovers
changed DOCX files on pull requests or pushes and uploads HTML/Word artifacts:

```yaml
steps:
  - uses: actions/checkout@v4
    with:
      fetch-depth: 0
  - uses: JSv4/docx-actions@main
```

Set `original` and `modified` together for an explicit pair. Other controls and
outputs are documented in [action.yml](action.yml). `manifest.json` records every
file's status, output paths, and compared commits. `html-preview: 'true'` makes
rendering failures fail the run while retaining results for other documents.
The reusable workflow enables this mode and renders one-sided snapshots.

The tested defaults pin Python-Redlines and its Docxodus engine wheel to 1.0.0,
and Docx2Html to 12.1.0. Comparison logic stays in those released dependencies;
this repository owns the GitHub integration and viewer.

## Execution and permissions

`pull_request_target` runs the workflow from the trusted base repository. The
comparison job has read-only repository access. It reads the head's DOCX blobs
with Git; it never checks out or executes PR code. This also works for fork PRs.
The publishing job uses the same revision of this repository as the called
workflow, downloads only artifacts from that caller's review workflow, validates
their metadata and paths, and renders them with scripts and external requests
disabled. Only the final deployment/comment steps use publishing credentials.

Keep that event and the reusable job intact. Don't add steps that execute the PR
head in a privileged workflow. Existing GitHub Pages environment protection
rules still apply; allow your default branch to deploy. Repository policies may
also restrict the requested `GITHUB_TOKEN` permissions.

## Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
python -m playwright install chromium
pytest -q
python tests/browser_smoke.py
```

Tests cover real engine comparisons, multiple documents and duplicate basenames,
snapshot handling, contextual rendering, safe HTML serialization, artifact
validation, and comment updates. MIT licensed; see [NOTICE.md](NOTICE.md) for
the original action and demo attribution.
