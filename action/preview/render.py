"""Publish formatted redlines and image/Markdown excerpts from the action's HTML."""

import argparse
from copy import deepcopy
from contextlib import ExitStack
from hashlib import sha256
import html
import json
import os
from pathlib import Path
import re
import shutil

from lxml import etree as ET
from playwright.sync_api import sync_playwright
from options import Options

ROOT = Path(__file__).resolve().parent
NS = {"h": "http://www.w3.org/1999/xhtml"}
H = "{" + NS["h"] + "}"


def plain(node):
    return " ".join("".join(node.itertext()).split())


def inline(node):
    """A small safe HTML subset that GitHub renders inside a Markdown comment."""
    tag = ET.QName(node).localname if isinstance(node.tag, str) else ''
    if tag == 'tr':
        return ' | '.join(inline(cell) for cell in node if ET.QName(cell).localname in ('td', 'th'))
    value = html.escape(node.text or "")
    for child in node:
        value += inline(child) + html.escape(child.tail or "")
    if tag in ('ins', 'del', 'strong', 'b', 'em', 'i', 'u', 'sup', 'sub') and value.strip():
        return f"<{tag}>{value}</{tag}>"
    if tag == "br":
        return " "
    return value


def serialize_html(root):
    """Write HTML5, keeping empty spans/anchors closed and void tags unpaired."""
    document = deepcopy(root)
    for node in document.iter():
        if isinstance(node.tag, str) and node.tag.startswith(H):
            node.tag = ET.QName(node).localname
    ET.cleanup_namespaces(document)
    return b"<!doctype html>\n" + ET.tostring(document, encoding="UTF-8", method="html")


def prepare_document(source, destination, latest=False):
    parser = ET.XMLParser(resolve_entities=False, no_network=True, remove_comments=True)
    root = ET.fromstring(source.read_bytes(), parser)
    for node in list(root.iter()):
        if not isinstance(node.tag, str):
            parent = node.getparent()
            if parent is not None:
                parent.remove(node)
            continue
        local = ET.QName(node).localname
        if local == 'style' and node.text:
            # HTML raw-text elements must not acquire markup when an XML
            # stylesheet contains a literal closing tag inside a CSS string.
            node.text = node.text.replace('<', r'\3c ')
        if local in ("script", "iframe", "object", "embed", "form", "base", "link") or (local == "meta" and node.get("http-equiv")):
            parent = node.getparent()
            if parent is not None:
                parent.remove(node)
            continue
        for name, value in list(node.attrib.items()):
            if name.lower().startswith("on") or name in ("srcdoc", "action"):
                del node.attrib[name]
            elif ET.QName(name).localname == 'href' and not value.startswith('#'):
                del node.attrib[name]
            elif ET.QName(name).localname == 'src' and not value.startswith('data:image/'):
                del node.attrib[name]
    head = root.find("h:head", NS)
    csp = ET.Element(H + "meta")
    csp.set("http-equiv", "Content-Security-Policy")
    csp.set("content", "default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; base-uri 'none'; form-action 'none'")
    head.insert(0, csp)
    style = ET.SubElement(head, H + "style")
    style.text = "html{background:#edf0f2} body{box-sizing:border-box;max-width:816px;margin:24px auto;padding:48px 64px;background:white;box-shadow:0 3px 20px #18232d12} [data-review-change]{scroll-margin:24px} @media(max-width:700px){body{margin:0;padding:24px 20px}}"
    changed = []
    for node in root.xpath("//h:p | //h:h1 | //h:h2 | //h:h3 | //h:tr", namespaces=NS):
        # Table rows have one anchor; avoid listing every cell paragraph again.
        if node.xpath("ancestor::h:tr", namespaces=NS):
            continue
        revisions = node.xpath(".//h:ins | .//h:del | .//*[contains(@class,'rev-')]", namespaces=NS)
        if not latest and not revisions and "rev-" not in node.get("class", ""):
            continue
        if not plain(node):
            continue
        number = len(changed) + 1
        identifier = f"review-{'passage' if latest else 'change'}-{number}"
        # Add an anchor without replacing any existing bookmark identifier.
        node.set("data-review-change", str(number))
        anchor = ET.Element(H + "a", id=identifier)
        node.insert(0, anchor)
        classes = " ".join(e.get("class", "") for e in node.iter())
        kind = "Moved" if "rev-move-" in classes else "Formatting" if "format-change" in classes else "Text"
        if latest:
            kind = 'Latest'
        accepted = " ".join("".join(node.xpath(".//text()[not(ancestor::h:del)]", namespaces=NS)).split())
        changed.append({"id": identifier, "number": number, "label": (accepted or plain(node))[:100],
                        "kind": kind, "markup": inline(node), "node": node})
    destination.write_bytes(serialize_html(root))
    return changed


def shell(title, content, prefix="./"):
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} · DOCX review</title><link rel="stylesheet" href="{prefix}assets/preview.css"></head>
<body>{content}</body></html>'''


def neighboring_block(node, direction):
    """Find adjacent document text in the same section or table, skipping blanks."""
    sibling = node.getprevious() if direction == "before" else node.getnext()
    while sibling is not None:
        if isinstance(sibling.tag, str) and plain(sibling):
            return sibling
        sibling = sibling.getprevious() if direction == "before" else sibling.getnext()
    return None


def surrounding_blocks(node, direction, count):
    blocks = []
    for _ in range(count):
        node = neighboring_block(node, direction)
        if node is None:
            break
        blocks.append(node)
    return list(reversed(blocks)) if direction == 'before' else blocks


def append_block(parent, node):
    fragment = deepcopy(node)
    if ET.QName(fragment).localname == "tr":
        source_table = node.xpath("ancestor::h:table[1]", namespaces=NS)
        table = ET.SubElement(parent, H + "table", **(dict(source_table[0].attrib) if source_table else {}))
        table.append(fragment)
    else:
        parent.append(fragment)


def contextual_excerpt(change, index, total, context_paragraphs=1):
    root = deepcopy(change["node"].getroottree().getroot())
    body = root.find("h:body", NS)
    for child in list(body):
        body.remove(child)
    style = ET.SubElement(root.find("h:head", NS), H + "style")
    style.text = (ROOT / "assets/excerpt.css").read_text()
    card = ET.SubElement(body, H + "section", {"class": "review-excerpt"})
    header = ET.SubElement(card, H + "header", {"class": "excerpt-header"})
    ET.SubElement(header, H + "span").text = f"EXCERPT {index} OF {total}"
    ET.SubElement(header, H + "strong").text = f"Passage {change['number']} · {change['kind']} change"
    before = surrounding_blocks(change['node'], 'before', context_paragraphs)
    after = surrounding_blocks(change['node'], 'after', context_paragraphs)
    for label, nodes in [("Before", before), ("Changed passage", [change["node"]]), ("After", after)]:
        if not nodes:
            continue
        role = "focus" if label == "Changed passage" else label.lower()
        section = ET.SubElement(card, H + "div", {"class": f"excerpt-section excerpt-{role}"})
        ET.SubElement(section, H + "div", {"class": "excerpt-label"}).text = label
        window = ET.SubElement(section, H + "div", {"class": "excerpt-window"})
        for node in nodes:
            append_block(window, node)
    return root


def select_excerpts(changes, count):
    if not changes or not count:
        return []
    # Prefer a dense text edit and a move to show distinct capabilities.
    ranked = sorted(changes, key=lambda c: len(c["node"].xpath(".//h:ins | .//h:del", namespaces=NS)), reverse=True)
    selected = [next((c for c in ranked if c["kind"] == "Text"), ranked[0])]
    second = next((c for c in changes if c["kind"] == "Moved" and len(c["label"]) > 30), None)
    if second is None:
        second = next((c for c in ranked if c != selected[0]), None)
    if second and second != selected[0]:
        selected.append(second)
    selected.extend(c for c in ranked if c not in selected)
    return selected[:count]


def render_images(browser, changes, output, count=2, context_paragraphs=1):
    selected = select_excerpts(changes, count)
    if not selected:
        return []
    images = []
    page = browser.new_page(viewport={"width": 1000, "height": 1000}, device_scale_factor=1.5)
    page.route(re.compile(r"https?://"), lambda route: route.abort())
    for index, change in enumerate(selected, 1):
        # Preserve the neighboring paragraphs and document styling around the edit.
        root = contextual_excerpt(change, index, len(selected), context_paragraphs)
        excerpt = output / f"excerpt-{index}.html"
        excerpt.write_bytes(serialize_html(root))
        page.goto(excerpt.resolve().as_uri())
        page.evaluate("document.fonts.ready")
        page.evaluate("""document.querySelectorAll('.excerpt-before .excerpt-window, .excerpt-after .excerpt-window').forEach(window => {
            window.toggleAttribute('data-clipped', window.firstElementChild.getBoundingClientRect().height > window.clientHeight + 1);
        })""")
        name = f"preview-{index}.png"
        page.locator(".review-excerpt").screenshot(path=str(output / name))
        # GitHub proxies comment images; a content hash prevents stale cached crops.
        fingerprint = sha256((output / name).read_bytes()).hexdigest()[:12]
        hashed_name = f"preview-{index}-{fingerprint}.png"
        (output / name).replace(output / hashed_name)
        name = hashed_name
        excerpt.unlink()
        images.append({"name": name, "anchor": change["id"], "label": change["label"], "kind": change["kind"]})
    page.close()
    return images


def artifact_file(source, relative):
    path = source / relative
    if path.is_symlink() or not path.resolve().is_relative_to(source.resolve()) or not path.is_file():
        raise ValueError(f'Invalid artifact file: {relative}')
    return path


def file_key(path):
    return sha256(path.encode('utf-8')).hexdigest()[:20]


def comment_marker(key):
    return f'<!-- docx-redlines-preview:{key} -->'


def status_label(record):
    if record.get('error'):
        return 'Comparison failed'
    if record['status'] == 'added':
        return 'Added document — full document, no previous version'
    if record['status'] == 'deleted':
        return 'Deleted document — last version, no replacement'
    if record.get('document') and record['status'] == 'renamed':
        return 'Renamed — document contents unchanged'
    return f"{record['revisions']} revisions" if record.get('revisions') is not None else 'Tracked changes'


def filename_markup(path):
    # Keep unusual Git filenames from splitting Markdown table rows or adding
    # markup. Full paths distinguish identical basenames in different folders.
    escaped = html.escape(path).replace('|', '&#124;').replace('\n', '&#10;').replace('\r', '&#13;')
    return f'<code>{escaped}</code>'


# The artifact URL is assigned by GitHub only after this review bundle is uploaded.
ARTIFACT_URL = 'https://docx-actions.invalid/review-artifact'


def comment_size(value):
    # Reserve room for the real Actions artifact URL before GitHub assigns it.
    return len(value) + value.count(ARTIFACT_URL) * (256 - len(ARTIFACT_URL))


def passage_list(changes, url, budget, max_passages=0, latest=False, downloads=True):
    passages, size = [], 0
    candidates = changes[:max_passages] if max_passages else changes
    for change in candidates:
        link = f" · [Open passage]({url}#{change['id']})" if url else ''
        kind = 'Passage' if latest else change['kind']
        passage = f"**{change['number']}. {kind}**{link}\n\n<p>{change['markup']}</p>\n"
        if size + len(passage) + 600 > budget:
            break
        passages.append(passage)
        size += len(passage) + 1
    if not passages:
        return ''
    noun = 'document passages' if latest else 'changed passages'
    label = f'Read all {len(changes)} {noun} directly in GitHub' if len(passages) == len(changes) else f'Read {len(passages)} of {len(changes)} {noun}'
    if not latest:
        label = 'Change log · ' + label
    else:
        label = 'Latest version · ' + label
    lines = ['<details>', f'<summary>{label}</summary>', '', *passages]
    if len(passages) < len(changes):
        target = url or ARTIFACT_URL
        description = 'Continue through all passages in the full viewer' if url else 'Download the complete review and change log'
        lines += [f'[{description}]({target})' if url or downloads else 'Remaining passages are retained in the review artifact.', '']
    lines += ['</details>', '']
    return '\n'.join(lines)


def text_excerpt(change, options):
    def context_markup(node):
        if len(plain(node)) <= 600:
            return inline(node)
        # Never concatenate deleted and inserted wording when shortening context.
        current = ' '.join(''.join(node.xpath('.//text()[not(ancestor::h:del)]', namespaces=NS)).split())
        return html.escape(current[:600]) + ('…' if len(current) > 600 else '')

    parts = []
    if 'node' in change:
        before = surrounding_blocks(change['node'], 'before', options.context_paragraphs)
        after = surrounding_blocks(change['node'], 'after', options.context_paragraphs)
        for node in before:
            parts.append(f'<p><em>{context_markup(node)}</em></p>')
    parts.append(f"<p>{change['markup']}</p>")
    if 'node' in change:
        for node in after:
            parts.append(f'<p><em>{context_markup(node)}</em></p>')
    return '\n\n'.join(parts)


def preview_section(document, opened=False, text_budget=0, options=None):
    options = options or Options(pages=bool(document.get('url')))
    record, url = document['record'], document.get('url') if options.pages else None
    latest = options.mode == 'latest' or (not record.get('redline') and bool(record.get('document')))
    changes = document.get('latest_changes', []) if options.mode == 'latest' else document['changes']
    if record.get('error') or not (record.get('redline') or record.get('latest') or (not options.pages and record.get('document'))):
        return ''
    if not options.inline_preview and (not options.change_log or latest):
        return ''
    label = 'Preview latest version:' if options.mode == 'latest' else 'Preview document:' if latest else 'Preview changes:'
    lines = ['<details open>' if opened else '<details>', f"<summary>{label} {filename_markup(record['path'])}</summary>", '']
    if record.get('previous_path'):
        lines += [f"Previously {filename_markup(record['previous_path'])}.", '']
    if options.inline_preview and options.pages and options.mode != 'latest':
        for index, picture in enumerate(document['images'], 1):
            link = f"{url}#{picture['anchor']}"
            lines += [f"[![{picture['kind']} change with preceding and following context]({url}{picture['name']})]({link})", '',
                      f'**[⤢ Expand excerpt {index} in full document ↗]({link})**', '']
    if text_budget < 600:
        return '\n'.join(lines + ['</details>', ''])
    latest_budget = text_budget // 3 if options.mode == 'both' and options.inline_preview else 0
    text_budget -= latest_budget
    used = 0
    if options.inline_preview and not (options.pages and document['images']) and not latest:
        allowance = text_budget // 3 if options.change_log else text_budget
        candidates = select_excerpts(changes, options.preview_count) if changes and 'node' in changes[0] else changes[:options.preview_count]
        for change in candidates:
            excerpt = f"**Passage {change['number']} · {change['kind']}**\n\n{text_excerpt(change, options)}\n"
            if used + len(excerpt) + 300 > allowance:
                break
            lines += [excerpt, '']
            used += len(excerpt) + 1
    if options.change_log and not latest or options.inline_preview and latest:
        passages = passage_list(changes, url, max(0, text_budget - used), options.max_passages, latest, options.downloads)
        if passages:
            lines += [passages, '']
        elif changes and text_budget:
            fallback = f'[Download the complete review and change log]({ARTIFACT_URL})' if options.downloads else 'Complete content is retained in the review artifact'
            lines += [fallback + ' (inline content exceeds the configured limit).', '']
        elif not changes and text_budget:
            lines += ['No text passages to display. Formatting and other document details remain available in the Word download.', '']
    if latest_budget:
        lines += [passage_list(document.get('latest_changes', []), document.get('latest_url') if options.pages else None, latest_budget,
                               options.max_passages, latest=True, downloads=options.downloads), '']
    lines += ['</details>', '']
    return '\n'.join(lines)


def review_status(record, mode):
    if record.get('error'):
        return 'Failed'
    if mode == 'latest':
        return 'Deleted — no latest version' if record['status'] == 'deleted' else 'Latest version'
    return {'added': 'Added', 'deleted': 'Deleted'}.get(record['status'], status_label(record))


def review_comment(item, documents, review_url=None, budget=58000, options=None):
    """One updating comment, with independent inline content and hosted views."""
    options = options or Options(pages=bool(review_url), comment_budget=budget)
    budget = options.comment_budget
    count = len(documents)
    title = f"## Word document review · {count} document{'s' if count != 1 else ''}"
    header = f"{comment_marker('index')}\n{title}\n\n"
    if options.pages:
        header += f'[Review all documents ↗]({review_url})\n\n'
    if options.downloads:
        header += f'[Download Word, HTML, and complete change logs]({ARTIFACT_URL})\n\n'
    if options.mode == 'latest':
        header += 'Latest document versions; no comparison was run.\n\n'
    footer = f"\n<sub>Source commit <code>{item['sha'][:12]}</code> · <a href=\"{html.escape(item['run_url'])}\">Action run</a></sub>\n"
    if not documents:
        return f"{comment_marker('index')}\n## Word document review\n\nNo changed Word documents.\n{footer}"
    table = '| Document | Changes | Actions |\n|---|---|---|\n'
    legend = '' if options.mode == 'latest' else '\n<sub>Underlined: inserted · Struck: deleted' + (' · Purple: moved' if options.pages else ' · Moves and formatting labeled in the change log') + '</sub>\n\n'
    rows, included = [], []
    size = comment_size(header + table + legend + footer) + 1000
    for document in documents:
        record, url = document['record'], document.get('url')
        if options.pages and url:
            label = 'View redline' if record.get('redline') and options.mode != 'latest' else 'View document'
            actions = f'[{label}]({url})'
            if options.mode == 'both' and document.get('latest_url'):
                actions += f" · [Latest version]({document['latest_url']})"
            if options.downloads:
                actions += f" · [Word]({url}{'latest.docx' if options.mode == 'latest' else 'redline.docx'})"
        else:
            actions = f'[Download results]({ARTIFACT_URL})' if options.downloads else '—'
            if record.get('error'):
                actions = f"[See result]({review_url or item['run_url']})"
        row = f"| {filename_markup(record['path'])} | {review_status(record, options.mode)} | {actions} |\n"
        minimum = preview_section(document, opened=count == 1, options=options)
        if size + comment_size(row + minimum) > budget:
            break
        rows.append(row)
        included.append(document)
        size += comment_size(row + minimum)
    target = review_url if options.pages else ARTIFACT_URL
    notice = '' if len(included) == count else f'\nShowing {len(included)} of {count} documents here. [Review all {count} documents]({target}) in the complete index.\n'
    if notice and not options.pages and not options.downloads:
        notice = f'\nShowing {len(included)} of {count} documents here. The complete index is retained in the review artifact.\n'
    sections = [preview_section(d, opened=count == 1, options=options) for d in included]
    if not any(sections):
        legend = ''
    prefix = header + table + ''.join(rows) + notice + legend
    remaining = budget - comment_size(prefix + footer + ''.join(sections)) - 500
    share = max(0, remaining // max(sum(bool(section) for section in sections), 1))
    sections = [preview_section(d, opened=count == 1, text_budget=share, options=options)
                if share >= 600 or options.pages and d['images'] else '' for d in included]
    body = prefix + ''.join(sections) + footer
    if comment_size(body) > budget:
        raise ValueError('Review comment exceeds its size budget')
    return body


def write_viewer(item, record, directory, relative, changes, latest, options, alternate=''):
    selector = ''.join(f'<option value="{c["id"]}">{c["number"]}. {html.escape(c["label"][:80])}</option>' for c in changes)
    prefix = '../' * len(Path(relative).parts)
    filename = 'latest.docx' if latest else 'redline.docx'
    download = f'<a class="button small" href="{filename}" download>Download Word</a>' if options.downloads else ''
    switch = f'<a href="{alternate}">{"View redline" if latest else "Latest version"}</a>' if alternate else ''
    label = 'Latest version' if latest else status_label(record)
    legend = '' if latest else '<div class="legend"><span class="insert">Inserted</span><span class="delete">Deleted</span><span class="move">Moved</span></div>'
    passage = 'passage' if latest else 'changed passage'
    content = f'''<header class="toolbar"><a class="brand" href="{prefix}index.html">DOCX <span>/ review</span></a>
<div class="toolbar-actions"><a href="{html.escape(item['pr_url'])}">Open in GitHub ↗</a>{switch}{download}</div></header>
<main class="review"><div class="review-title"><div><p class="eyebrow">{html.escape(item['title'])}</p><h1>{html.escape(record['path'])}</h1><p class="muted">{label} · commit {item['sha'][:12]}</p></div>{legend}</div>
<nav class="change-nav" aria-label="Navigate document"><button id="previous" aria-label="Previous {passage}">← Previous</button><select id="changes" aria-label="Document passage">{selector}</select><button id="next" aria-label="Next {passage}">Next →</button><span id="position" aria-live="polite"></span></nav>
<iframe id="document" title="{'Latest document version' if latest else 'Full document with tracked changes'}" sandbox="allow-same-origin" src="document.html"></iframe></main>
<script src="{prefix}assets/viewer.js"></script>'''
    (directory / 'index.html').write_text(shell(record['path'], content, prefix), encoding='utf-8')


def build(catalog_path, output, base_url, comments_path, options=None):
    from urllib.parse import urlsplit
    options = options or Options(pages=bool(base_url))
    if options.pages:
        parsed = urlsplit(base_url)
        if parsed.scheme != 'https' or not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError('The Pages base URL must be an HTTPS URL without a query or fragment')
    base_url = base_url.rstrip('/') if options.pages else ''
    catalog = json.loads(catalog_path.read_text())
    output.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ROOT / 'assets', output / 'assets', dirs_exist_ok=True)
    cards, comments = [], []
    with ExitStack() as stack:
        browser = None
        if options.pages and options.inline_preview and options.preview_count and options.mode != 'latest':
            playwright = stack.enter_context(sync_playwright())
            browser_args = {'headless': True}
            if os.environ.get('CHROME_PATH'):
                browser_args['executable_path'] = os.environ['CHROME_PATH']
            browser = playwright.chromium.launch(**browser_args)
            stack.callback(browser.close)
        for item in catalog:
            source = Path(item['input_dir']).resolve()
            if not re.fullmatch(r'[0-9a-f]{40}', item['sha']):
                raise ValueError('Invalid source commit')
            pr = int(item['pr'])
            if pr < 1:
                raise ValueError('Invalid PR number')
            manifest = json.loads(artifact_file(source, 'manifest.json').read_text())
            if manifest.get('mode', 'redline') != options.mode:
                raise ValueError('Comparison mode does not match the requested review; recompute this PR')
            records = manifest['files']
            links, documents, report = [], [], [f'# PR #{pr}: {html.escape(item["title"])}', f'Source commit: {item["sha"]}', '']
            pr_relative = f'pr/{pr}/{item["sha"]}'
            pr_url = f'{base_url}/{pr_relative}/' if options.pages else None
            for record in records:
                key = file_key(record['path'])
                relative = f'{pr_relative}/file-{key}'
                directory = output / relative
                directory.mkdir(parents=True, exist_ok=True)
                url, latest_url, changes, latest_changes, images = None, None, [], [], []
                document_name = record.get('redline') or record.get('document')
                if record.get('html') and document_name and options.mode != 'latest':
                    changes = prepare_document(artifact_file(source, record['html']), directory / 'document.html', latest=not record.get('redline'))
                    shutil.copyfile(artifact_file(source, document_name), directory / 'redline.docx')
                    if browser and record.get('redline'):
                        images = render_images(browser, changes, directory, options.preview_count, options.context_paragraphs)
                    url = f'{base_url}/{relative}/' if options.pages else None
                    write_viewer(item, record, directory, relative, changes, False, options,
                                 'latest/' if record.get('latest_html') else '')
                if record.get('latest') and record.get('latest_html'):
                    latest_relative = relative if options.mode == 'latest' else relative + '/latest'
                    latest_dir = output / latest_relative
                    latest_dir.mkdir(parents=True, exist_ok=True)
                    latest_changes = prepare_document(artifact_file(source, record['latest_html']), latest_dir / 'document.html', latest=True)
                    shutil.copyfile(artifact_file(source, record['latest']), latest_dir / 'latest.docx')
                    latest_url = f'{base_url}/{latest_relative}/' if options.pages else None
                    write_viewer(item, record, latest_dir, latest_relative, latest_changes, True, options,
                                 '../' if document_name and options.mode == 'both' else '')
                    if options.mode == 'latest':
                        url = latest_url
                # Keep downloadable Word results even if HTML rendering failed.
                for field in ('redline', 'document', 'latest'):
                    if record.get(field):
                        name = 'latest.docx' if field == 'latest' else 'redline.docx'
                        if not (directory / name).exists():
                            shutil.copyfile(artifact_file(source, record[field]), directory / name)
                report.extend([f"## {filename_markup(record['path'])}", review_status(record, options.mode), ''])
                entries = latest_changes if options.mode == 'latest' else changes
                document_log = []
                for change in entries:
                    kind = 'Passage' if options.mode == 'latest' else change['kind']
                    document_log.extend([f"### {change['number']}. {kind}", f"<p>{change['markup']}</p>", ''])
                if not entries:
                    document_log.append('No text passages. See the file status above and the Word result, when available.')
                (directory / 'CHANGELOG.md').write_text('\n'.join(document_log), encoding='utf-8')
                report.extend(document_log)
                label = html.escape(record['path'])
                available = (directory / 'index.html').exists()
                links.append(f'<li><a href="file-{key}/">{label}</a> — {review_status(record, options.mode)}</li>' if available else f'<li>{label} — {review_status(record, options.mode)}</li>')
                documents.append({'record': record, 'url': url, 'latest_url': latest_url,
                                  'changes': changes, 'latest_changes': latest_changes, 'images': images})
            directory = output / pr_relative
            directory.mkdir(parents=True, exist_ok=True)
            (directory / 'CHANGELOG.md').write_text('\n'.join(report), encoding='utf-8')
            file_list = '<ul>' + ''.join(links) + '</ul>' if links else '<p>No changed Word documents.</p>'
            content = f'<main class="landing"><h1>PR #{pr}: {html.escape(item["title"])}</h1><p>Source commit {item["sha"][:12]} · {len(records)} documents</p>{file_list}<a href="{html.escape(item["pr_url"])}">Open pull request ↗</a></main>'
            (directory / 'index.html').write_text(shell(item['title'], content, '../../../'), encoding='utf-8')
            cards.append(f'<article class="card"><div class="card-content"><p class="eyebrow">Pull request #{pr}</p><h2>{html.escape(item["title"])}</h2><p>{len(records)} Word documents · commit {item["sha"][:12]}</p><a class="button" href="{pr_relative}/">Review documents ↗</a></div></article>')
            if options.comments and item['sha'] == item['current_head']:
                comments.append({'pr': pr, 'sha': item['sha'], 'file_count': len(records),
                                 'body': review_comment(item, documents, pr_url, options=options)})
    content = f'''<header class="toolbar"><a class="brand" href="./">DOCX <span>/ review</span></a></header>
<main class="landing"><div class="hero"><p class="eyebrow">WORD DOCUMENT REVIEW · GITHUB ACTIONS</p><h1>Review your Word documents<br>with their full context.</h1><p class="intro">Review changed Word documents with contextual excerpts, full browser views, and native tracked changes.</p></div><div class="cards">{''.join(cards) or '<p>No open pull requests with document previews.</p>'}</div><footer>Generated with Python-Redlines and Docxodus. Each comparison identifies its source commit. Previews are retained while their source artifacts are available.</footer></main>'''
    (output / 'index.html').write_text(shell('Word document previews', content), encoding='utf-8')
    (output / 'docx-actions-site.json').write_text(json.dumps({'generator': 'JSv4/docx-actions', 'repository': os.environ.get('GITHUB_REPOSITORY', '')}), encoding='utf-8')
    (output / '.nojekyll').touch()
    comments_path.write_text(json.dumps(comments, indent=2) + '\n', encoding='utf-8')
    print(f'Built {len(cards)} reviews in {output}; Pages {"enabled" if options.pages else "disabled"}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', type=Path, default=Path('_preview_inputs/catalog.json'))
    parser.add_argument('--output', type=Path, default=Path('_site'))
    parser.add_argument('--base-url', default='')
    parser.add_argument('--comments', type=Path, default=Path('_preview_comments.json'))
    args = parser.parse_args()
    build(args.catalog, args.output, args.base_url, args.comments, Options.from_env())
