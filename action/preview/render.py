"""Publish formatted redlines and image/Markdown excerpts from the action's HTML."""

import argparse
from copy import deepcopy
from hashlib import sha256
import html
import json
import os
from pathlib import Path
import re
import shutil

from lxml import etree as ET
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
NS = {"h": "http://www.w3.org/1999/xhtml"}
H = "{" + NS["h"] + "}"


def plain(node):
    return " ".join("".join(node.itertext()).split())


def inline(node):
    """A small safe HTML subset that GitHub renders inside a Markdown comment."""
    value = html.escape(node.text or "")
    for child in node:
        value += inline(child) + html.escape(child.tail or "")
    tag = ET.QName(node).localname if isinstance(node.tag, str) else ''
    if tag in ("ins", "del") and value.strip():
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


def prepare_document(source, destination):
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
        if not revisions and "rev-" not in node.get("class", ""):
            continue
        if not plain(node):
            continue
        number = len(changed) + 1
        identifier = f"review-change-{number}"
        # Add an anchor without replacing any existing bookmark identifier.
        node.set("data-review-change", str(number))
        anchor = ET.Element(H + "a", id=identifier)
        node.insert(0, anchor)
        classes = " ".join(e.get("class", "") for e in node.iter())
        kind = "Moved" if "rev-move-" in classes else "Formatting" if "format-change" in classes else "Text"
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


def append_block(parent, node):
    fragment = deepcopy(node)
    if ET.QName(fragment).localname == "tr":
        source_table = node.xpath("ancestor::h:table[1]", namespaces=NS)
        table = ET.SubElement(parent, H + "table", **(dict(source_table[0].attrib) if source_table else {}))
        table.append(fragment)
    else:
        parent.append(fragment)


def contextual_excerpt(change, index, total):
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
    before = neighboring_block(change["node"], "before")
    after = neighboring_block(change["node"], "after")
    for label, node in [("Before", before), ("Changed passage", change["node"]), ("After", after)]:
        if node is None:
            continue
        role = "focus" if label == "Changed passage" else label.lower()
        section = ET.SubElement(card, H + "div", {"class": f"excerpt-section excerpt-{role}"})
        ET.SubElement(section, H + "div", {"class": "excerpt-label"}).text = label
        window = ET.SubElement(section, H + "div", {"class": "excerpt-window"})
        append_block(window, node)
    return root


def render_images(browser, changes, output):
    if not changes:
        return []
    # Prefer a dense text edit and a move to show distinct capabilities.
    ranked = sorted(changes, key=lambda c: len(c["node"].xpath(".//h:ins | .//h:del", namespaces=NS)), reverse=True)
    selected = [next((c for c in ranked if c["kind"] == "Text"), ranked[0])]
    second = next((c for c in changes if c["kind"] == "Moved" and len(c["label"]) > 30), None)
    if second is None:
        second = next((c for c in ranked if c != selected[0]), None)
    if second and second != selected[0]:
        selected.append(second)
    images = []
    page = browser.new_page(viewport={"width": 1000, "height": 1000}, device_scale_factor=1.5)
    page.route(re.compile(r"https?://"), lambda route: route.abort())
    for index, change in enumerate(selected, 1):
        # Preserve the neighboring paragraphs and document styling around the edit.
        root = contextual_excerpt(change, index, len(selected))
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


def passage_list(changes, url, budget):
    passages, size = [], 0
    for change in changes:
        passage = f"**{change['number']}. {change['kind']}** · [Open passage]({url}#{change['id']})\n\n<p>{change['markup']}</p>\n"
        if size + len(passage) + 500 > budget:
            break
        passages.append(passage)
        size += len(passage) + 1
    if not passages:
        return ''
    label = f'Read all {len(changes)} changed passages directly in GitHub' if len(passages) == len(changes) else f'Read {len(passages)} of {len(changes)} changed passages'
    lines = ['<details>', f'<summary>{label}</summary>', '', *passages]
    if len(passages) < len(changes):
        lines += [f'[Continue through all passages in the full viewer]({url})', '']
    lines += ['</details>', '']
    return '\n'.join(lines)


def preview_section(document, opened=False, text_budget=0):
    record, url = document['record'], document['url']
    if not url or not record.get('redline'):
        return ''
    lines = ['<details open>' if opened else '<details>',
             f"<summary>Preview changes: {filename_markup(record['path'])}</summary>", '']
    if record.get('previous_path'):
        lines += [f"Previously {filename_markup(record['previous_path'])}.", '']
    for index, picture in enumerate(document['images'], 1):
        link = f"{url}#{picture['anchor']}"
        lines += [f"[![{picture['kind']} change with preceding and following context]({url}{picture['name']})]({link})", '',
                  f'**[⤢ Expand excerpt {index} in full document ↗]({link})**', '']
    if not document['images']:
        lines += [f'[Review changes in the full document]({url})', '']
    passages = passage_list(document['changes'], url, text_budget)
    if passages:
        lines += [passages, '']
    lines += ['</details>', '']
    return '\n'.join(lines)


def review_comment(item, documents, review_url, budget=58000):
    """One summary and expandable previews, fairly bounded across documents."""
    count = len(documents)
    title = f"## Word document review · {count} document{'s' if count != 1 else ''}"
    header = f"{comment_marker('index')}\n{title}\n\n[Review all documents ↗]({review_url})\n\n"
    footer = f"\n<sub>Compared commit <code>{item['sha'][:12]}</code> · <a href=\"{html.escape(item['run_url'])}\">Action run</a></sub>\n"
    if not documents:
        return f"{comment_marker('index')}\n## Word document review\n\nNo changed Word documents.\n{footer}"
    table = '| Document | Changes | Actions |\n|---|---|---|\n'
    legend = '\n<sub>Underlined: inserted · Struck: deleted · Purple: moved</sub>\n\n'
    rows, included = [], []
    size = len(header + table + legend + footer) + 1000
    # Reserve space for every included document's links and image previews
    # before allocating the optional text passages. Very large PRs retain a
    # complete browser index even when GitHub's comment limit is reached.
    for document in documents:
        record, url = document['record'], document['url']
        status = {'added': 'Added', 'deleted': 'Deleted'}.get(record['status'], status_label(record))
        if record.get('error'):
            status = 'Failed'
        if url:
            label = 'View redline' if record.get('redline') else 'View document'
            actions = f'[{label}]({url}) · [Word]({url}redline.docx)'
        else:
            actions = f"[See result]({review_url})"
        row = f"| {filename_markup(record['path'])} | {status} | {actions} |\n"
        minimum = preview_section(document, opened=count == 1)
        if size + len(row) + len(minimum) > budget:
            break
        rows.append(row)
        included.append(document)
        size += len(row) + len(minimum)
    notice = '' if len(included) == count else f'\nShowing {len(included)} of {count} documents here. [Review all {count} documents]({review_url}) in the complete index.\n'
    sections = [preview_section(d, opened=count == 1) for d in included]
    if not any(sections):
        legend = ''
    prefix = header + table + ''.join(rows) + notice + legend
    remaining = budget - len(prefix + footer + ''.join(sections))
    allocations = sum(bool(section) for section in sections)
    share = max(0, remaining // max(allocations, 1))
    sections = [preview_section(d, opened=count == 1, text_budget=share) for d in included]
    body = prefix + ''.join(sections) + footer
    if len(body) > budget:
        raise ValueError('Review comment exceeds its size budget')
    return body


def build(catalog_path, output, base_url, comments_path):
    from urllib.parse import urlsplit
    parsed = urlsplit(base_url)
    if parsed.scheme != 'https' or not parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError('The Pages base URL must be an HTTPS URL without a query or fragment')
    base_url = base_url.rstrip('/')
    catalog = json.loads(catalog_path.read_text())
    output.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ROOT / 'assets', output / 'assets', dirs_exist_ok=True)
    cards, comments = [], []
    with sync_playwright() as playwright:
        browser_args = {'headless': True}
        if os.environ.get('CHROME_PATH'):
            browser_args['executable_path'] = os.environ['CHROME_PATH']
        browser = playwright.chromium.launch(**browser_args)
        for item in catalog:
            source = Path(item['input_dir']).resolve()
            if not re.fullmatch(r'[0-9a-f]{40}', item['sha']):
                raise ValueError('Invalid source commit')
            pr = int(item['pr'])
            if pr < 1:
                raise ValueError('Invalid PR number')
            records = json.loads(artifact_file(source, 'manifest.json').read_text())['files']
            links, documents = [], []
            pr_relative = f'pr/{pr}/{item["sha"]}'
            pr_url = f'{base_url}/{pr_relative}/'
            for record in records:
                key = file_key(record['path'])
                relative = f'{pr_relative}/file-{key}'
                url, changes, images = None, [], []
                document_name = record.get('redline') or record.get('document')
                if record.get('html') and document_name:
                    directory = output / relative
                    directory.mkdir(parents=True, exist_ok=True)
                    changes = prepare_document(artifact_file(source, record['html']), directory / 'document.html')
                    shutil.copyfile(artifact_file(source, document_name), directory / 'redline.docx')
                    if record.get('redline'):
                        images = render_images(browser, changes, directory)
                    url = f'{base_url}/{relative}/'
                    options = ''.join(f'<option value="{c["id"]}">{c["number"]}. {html.escape(c["label"][:80])}</option>' for c in changes)
                    prefix = '../' * len(Path(relative).parts)
                    content = f'''<header class="toolbar"><a class="brand" href="{prefix}index.html">DOCX <span>/ review</span></a>
<div class="toolbar-actions"><a href="{html.escape(item['pr_url'])}">Open in GitHub ↗</a><a class="button small" href="redline.docx" download>Download Word</a></div></header>
<main class="review"><div class="review-title"><div><p class="eyebrow">{html.escape(item['title'])}</p><h1>{html.escape(record['path'])}</h1><p class="muted">{status_label(record)} · commit {item['sha'][:12]}</p></div><div class="legend"><span class="insert">Inserted</span><span class="delete">Deleted</span><span class="move">Moved</span></div></div>
<nav class="change-nav" aria-label="Navigate changes"><button id="previous" aria-label="Previous changed passage">← Previous</button><select id="changes" aria-label="Changed passage">{options}</select><button id="next" aria-label="Next changed passage">Next →</button><span id="position" aria-live="polite"></span></nav>
<iframe id="document" title="Full document with tracked changes" sandbox="allow-same-origin" src="document.html"></iframe></main>
<script src="{prefix}assets/viewer.js"></script>'''
                    (directory / 'index.html').write_text(shell(record['path'], content, prefix), encoding='utf-8')
                label = html.escape(record['path'])
                links.append(f'<li><a href="file-{key}/">{label}</a> — {status_label(record)}</li>' if url else f'<li>{label} — {status_label(record)}</li>')
                documents.append({'record': record, 'url': url, 'changes': changes, 'images': images})
            directory = output / pr_relative
            directory.mkdir(parents=True, exist_ok=True)
            file_list = '<ul>' + ''.join(links) + '</ul>' if links else '<p>No changed Word documents.</p>'
            content = f'<main class="landing"><h1>PR #{pr}: {html.escape(item["title"])}</h1><p>Compared commit {item["sha"][:12]} · {len(records)} documents</p>{file_list}<a href="{html.escape(item["pr_url"])}">Open pull request ↗</a></main>'
            (directory / 'index.html').write_text(shell(item['title'], content, '../../../'), encoding='utf-8')
            cards.append(f'<article class="card"><div class="card-content"><p class="eyebrow">Pull request #{pr}</p><h2>{html.escape(item["title"])}</h2><p>{len(records)} Word documents · commit {item["sha"][:12]}</p><a class="button" href="{pr_relative}/">Review documents ↗</a></div></article>')
            if item['sha'] == item['current_head']:
                comments.append({'pr': pr, 'sha': item['sha'], 'file_count': len(records),
                                 'body': review_comment(item, documents, pr_url)})
        browser.close()
    content = f'''<header class="toolbar"><a class="brand" href="./">DOCX <span>/ review</span></a></header>
<main class="landing"><div class="hero"><p class="eyebrow">WORD DOCUMENT REVIEW · GITHUB ACTIONS</p><h1>Read the changes.<br>Keep the document.</h1><p class="intro">Review changed Word documents with contextual excerpts, full browser views, and native tracked changes.</p></div><div class="cards">{''.join(cards) or '<p>No open pull requests with document previews.</p>'}</div><footer>Generated with Python-Redlines and Docxodus. Each comparison identifies its source commit. Previews are retained while their source artifacts are available.</footer></main>'''
    (output / 'index.html').write_text(shell('Word document previews', content), encoding='utf-8')
    (output / '.nojekyll').touch()
    comments_path.write_text(json.dumps(comments, indent=2) + '\n', encoding='utf-8')
    print(f'Built {len(cards)} comparisons in {output}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', type=Path, default=Path('_preview_inputs/catalog.json'))
    parser.add_argument('--output', type=Path, default=Path('_site'))
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--comments', type=Path, default=Path('_preview_comments.json'))
    args = parser.parse_args()
    build(args.catalog, args.output, args.base_url, args.comments)
