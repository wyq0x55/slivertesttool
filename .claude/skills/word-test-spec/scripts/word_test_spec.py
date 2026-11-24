#!/usr/bin/env python3
"""Extract and query test specification tables in local DOCX packages."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import posixpath
import re
import sys
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile

W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
A = '{http://schemas.openxmlformats.org/drawingml/2006/main}'
WP = '{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}'
R = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
IMAGE_REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/image'
REL = '{http://schemas.openxmlformats.org/package/2006/relationships}'
VERSION = '1.3.0'
SCHEMA_VERSION = 1
MAX_SOURCE = 64 * 1024**2
MAX_XML = 16 * 1024**2
MAX_EXPANDED = 256 * 1024**2
HEADING = re.compile(r'^([0-9]+(?:\.[0-9]+)*)(?:[.、)．]\s*|\s+)(\S.*)$')
TYPED_VALUE = re.compile(r'^[A-Za-z][A-Za-z0-9 _-]{0,20};;(.*)$', re.DOTALL)
SENTENCE_END = ('。', '.', '．', '!', '！', '?', '？')
FIELDS = {
    'id': ('id', '項目id', '要求id'),
    'reference_id': ('reference id', 'reference id (upper requirement)', 'ref', '参照id', '上位要求id'),
    'asil': ('asil',),
    'classification': ('classification', '分類', '区分'),
    'constraints': ('constraints', 'constraint', '制約', '制約条件'),
    'output_if': ('output i/f', 'output if', 'output interface', '出力i/f', '出力インターフェース'),
    'description': ('description', '説明', '内容', '詳細'),
}
LABELS = {label: field for field, labels in FIELDS.items() for label in labels}
DELETED = {W + name for name in ('del', 'moveFrom')}
WRAPPERS = {W + name for name in ('ins', 'moveTo', 'sdt', 'sdtContent', 'customXml', 'smartTag')}
STRUCTURAL = {W + name for name in ('p', 'tbl', 'tr', 'tc')}
REVISION_TAGS = DELETED | {W + name for name in (
    'ins', 'moveTo', 'cellIns', 'cellDel', 'cellMerge', 'pPrChange', 'rPrChange',
    'sectPrChange', 'tblPrChange', 'tblGridChange', 'trPrChange', 'tcPrChange',
    'numberingChange',
)}
PART = 'word/document.xml'
REL_PART = 'word/_rels/document.xml.rels'


def children(node, tag=None):
    """Yield body, cell, and row children through visible content wrappers."""
    for child in node:
        if child.tag in WRAPPERS:
            yield from children(child, tag)
        elif child.tag in STRUCTURAL and (tag is None or child.tag == tag):
            yield child


def strike_on(node):
    """Whether a run or paragraph mark carries strikethrough formatting."""
    for path in (f'{W}pPr/{W}rPr', f'{W}rPr'):
        for name in ('strike', 'dstrike'):
            element = node.find(f'{path}/{W}{name}')
            if element is None:
                continue
            if element.get(W + 'val') == '0':
                return False
            return True
    return False


def inline_content(node, struck=False):
    for child in node:
        if child.tag == W + 't':
            yield (child.text or '', struck)
        elif child.tag in {W + 'tab', W + 'ptab'}:
            yield ('\t', struck)
        elif child.tag in {W + 'br', W + 'cr'}:
            yield ('\n', struck)
        elif child.tag == W + 'noBreakHyphen':
            yield ('‑', struck)
        elif child.tag in {W + 'softHyphen', W + 'sym'}:
            if child.tag == W + 'softHyphen':
                yield ('­', struck)
            else:
                yield child
        elif child.tag in {W + 'drawing', W + 'pict', W + 'object', W + 'altChunk'}:
            yield child
        elif child.tag in WRAPPERS or child.tag in {W + 'hyperlink', W + 'fldSimple'}:
            yield from inline_content(child, struck)
        elif child.tag == W + 'r':
            yield from inline_content(child, strike_on(child) or struck)
        elif child.tag == W + 'ruby':
            base = child.find(W + 'rubyBase')
            if base is not None:
                yield from inline_content(base, struck)
        elif child.tag.endswith('}AlternateContent'):
            yield child


def paragraph_text(paragraph):
    return ''.join(text for part in inline_content(paragraph) if isinstance(part, tuple) for text, _struck in (part,))


def typed_value(text):
    """Strip a requirement-tool type prefix such as ``ID;;`` or ``REF;;``."""
    match = TYPED_VALUE.fullmatch(text.strip())
    return match.group(1).strip() if match else text.strip()


def label_for(cell):
    text = ' '.join(paragraph_text(p) for p in children(cell, W + 'p'))
    normalized = unicodedata.normalize('NFKC', text).strip().rstrip(':')
    return LABELS.get(' '.join(normalized.casefold().split()))


def xml_part(package, name, required=False):
    try:
        entry = package.getinfo(name)
    except KeyError:
        if required:
            raise ValueError(f'Missing DOCX package part: {name}') from None
        return None
    if entry.file_size > MAX_XML:
        raise ValueError(f'XML part exceeds size limit: {name}')
    data = package.read(entry)
    if b'<!DOCTYPE' in data.replace(b'\0', b'').upper():
        raise ValueError(f'DTD is not supported in XML part: {name}')
    root = ET.fromstring(data)
    stack = [(root, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > 80:
            raise ValueError(f'XML nesting exceeds limit: {name}')
        stack.extend((child, depth + 1) for child in node)
    return root


def w_value(node, path):
    element = node.find(path)
    return element.get(W + 'val') if element is not None else None


def plain_text(blocks):
    parts = []
    for block in blocks:
        if block['type'] in {'paragraph', 'list_item'}:
            parts.append(''.join(part['text'] for part in block['content'] if part['type'] == 'text'))
        elif block['type'] == 'table':
            parts.extend('\t'.join(plain_text(cell['blocks']) for cell in row['cells']) for row in block['rows'])
    return '\n'.join(parts)


class Extractor:
    def __init__(self, package):
        self.package = package
        self.warnings = []
        self.assets = {}
        self.table_index = None
        self.image_count = 0
        relationships = xml_part(package, REL_PART)
        self.relationships = {
            rel.get('Id'): rel for rel in relationships.findall(REL + 'Relationship')
        } if relationships is not None else {}
        styles = xml_part(package, 'word/styles.xml')
        self.styles = {
            style.get(W + 'styleId'): style for style in styles.findall(W + 'style')
            if style.get(W + 'type') == 'paragraph'
        } if styles is not None else {}

    def warn(self, code, message, *, review=True):
        warning = {'code': code, 'message': message, 'requires_review': review}
        if self.table_index is not None:
            warning['table_index'] = self.table_index
        self.warnings.append(warning)

    def style_chain(self, paragraph):
        style_id = w_value(paragraph, f'{W}pPr/{W}pStyle')
        seen = set()
        while style_id in self.styles and style_id not in seen:
            seen.add(style_id)
            style = self.styles[style_id]
            yield style
            style_id = w_value(style, W + 'basedOn')

    def heading(self, paragraph):
        text = paragraph_text(paragraph).strip()
        if not text:
            return None
        match = HEADING.fullmatch(text)
        if (match and len(text) <= 80 and not text.endswith(SENTENCE_END)
                and '\t' not in text and len(match.group(2)) <= 40):
            return (*match.groups(), text)
        for node in (paragraph, *self.style_chain(paragraph)):
            level = w_value(node, f'{W}pPr/{W}outlineLvl')
            if level is not None:
                return ('', text, text) if level in set('012345678') else None
        return None

    def image(self, relationship_id):
        rel = self.relationships.get(relationship_id)
        target = rel.get('Target', '') if rel is not None else ''
        if (rel is None or rel.get('Type') != IMAGE_REL
                or rel.get('TargetMode') == 'External' or not target or '\\' in target):
            self.warn('IMAGE_EXPORT_FAILED', f'Image relationship is missing or not an embedded image: {relationship_id}')
            return {'type': 'unsupported', 'object': 'image', 'relationship_id': relationship_id}
        path = posixpath.normpath(posixpath.join('word', target)).lstrip('/')
        try:
            if not path.startswith('word/media/'):
                raise ValueError('Image is outside word/media')
            image = self.package.read(path)
        except (BadZipFile, KeyError, ValueError, RuntimeError) as exc:
            self.warn('IMAGE_EXPORT_FAILED', f'{relationship_id}: {exc}')
            return {'type': 'unsupported', 'object': 'image', 'relationship_id': relationship_id}
        digest = hashlib.sha256(image).hexdigest()
        extension = PurePosixPath(path).suffix.lower()
        if not re.fullmatch(r'\.[a-z0-9]{1,8}', extension):
            extension = '.bin'
        filename = digest + extension
        self.assets.setdefault(filename, image)
        self.image_count += 1
        return {'type': 'image', 'path': f'assets/{filename}', 'sha256': digest, 'relationship_id': relationship_id}

    def object_content(self, node):
        if node.tag.endswith('}AlternateContent'):
            drawing = node.find('.//' + W + 'drawing')
            if drawing is not None:
                return self.object_content(drawing)
        if node.tag == W + 'drawing':
            if node.find('.//' + WP + 'anchor') is not None:
                self.warn('LAYOUT_UNCERTAIN', 'A floating drawing is positioned at its Word anchor.')
            if node.find('.//' + W + 'txbxContent') is not None:
                self.warn('UNSUPPORTED_OBJECT', 'Text box content inside a drawing is not extracted.')
            blips = node.findall('.//' + A + 'blip')
            if blips:
                return [self.image(blip.get(R + 'embed') or blip.get(R + 'link')) for blip in blips]
        kind = node.tag.rsplit('}', 1)[-1]
        self.warn('UNSUPPORTED_OBJECT', f'Unsupported Word object: {kind}')
        return [{'type': 'unsupported', 'object': kind}]

    def blocks(self, node):
        blocks = []
        for child in children(node):
            if child.tag == W + 'tbl':
                blocks.append(self.nested_table(child))
                continue
            if child.tag != W + 'p':
                continue
            # A struck paragraph is content the author marked for deletion, so it is
            # omitted entirely rather than kept where it could be quoted as a requirement.
            if strike_on(child) or any(strike_on(style) for style in self.style_chain(child)):
                self.warn('STRUCK_TEXT', 'A strikethrough paragraph was omitted as deleted content.')
                continue
            content = []
            for part in inline_content(child):
                if isinstance(part, tuple):
                    text, part_struck = part
                    if part_struck:
                        self.warn('STRUCK_TEXT', 'Strikethrough text inside a paragraph was omitted.')
                        continue
                    if content and content[-1]['type'] == 'text':
                        content[-1]['text'] += text
                    elif text:
                        content.append({'type': 'text', 'text': text})
                else:
                    content.extend(self.object_content(part))
            if not content:
                continue
            block = {'type': 'paragraph', 'content': content}
            for styled in (child, *self.style_chain(child)):
                numbering = styled.find(f'{W}pPr/{W}numPr')
                if numbering is None:
                    continue
                num_id = w_value(numbering, W + 'numId')
                if num_id != '0':
                    block.update(type='list_item', level=int(w_value(numbering, W + 'ilvl') or 0), numbering_id=num_id)
                break
            blocks.append(block)
        return blocks

    def nested_table(self, table):
        rows = []
        for row in children(table, W + 'tr'):
            column = int(w_value(row, f'{W}trPr/{W}gridBefore') or 0) + 1
            cells = []
            for cell in children(row, W + 'tc'):
                span = int(w_value(cell, f'{W}tcPr/{W}gridSpan') or 1)
                merge = cell.find(f'{W}tcPr/{W}vMerge')
                cells.append({'column': column, 'colspan': span,
                              'vertical_merge': (merge.get(W + 'val') or 'continue') if merge is not None else None,
                              'blocks': self.blocks(cell)})
                column += span
            rows.append({'is_header': row.find(f'{W}trPr/{W}tblHeader') is not None
                         and w_value(row, f'{W}trPr/{W}tblHeader') not in {'0', 'false', 'off'}, 'cells': cells})
        return {'type': 'table', 'rows': rows}

    def item(self, table, heading):
        fields = {}
        duplicates = set()
        for row in children(table, W + 'tr'):
            cells = list(children(row, W + 'tc'))
            index = 0
            while index < len(cells):
                field = label_for(cells[index])
                if not field:
                    index += 1
                    continue
                following = cells[index + 1] if index + 1 < len(cells) else None
                if following is not None and label_for(following):
                    following = None
                else:
                    index += 1
                if field in fields:
                    duplicates.add(field)
                else:
                    fields[field] = following
                index += 1
        if len(fields) < 2 or not {'id', 'description'}.intersection(fields):
            if fields:
                self.warn('TABLE_NOT_EXTRACTED',
                          'Table has recognized labels but is not an item: ' + ', '.join(sorted(fields)))
            return None

        section, title, heading_text = heading or ('', '', '')
        item = {'id': '', 'section_number': section, 'title': title,
                'source': {'part': PART, 'table_index': self.table_index, 'heading_text': heading_text}}
        for field in sorted(duplicates):
            self.warn('DUPLICATE_FIELD', f'Field label occurs more than once in this table: {field}')
        for field in FIELDS:
            cell = fields.get(field)
            blocks = self.blocks(cell) if cell is not None else []
            text = plain_text(blocks)
            if field == 'description':
                item['description'] = {'plain_text': text, 'blocks': blocks}
            elif field == 'reference_id':
                item['reference_id_raw'] = text
                item['reference_ids'] = []
                for line in text.splitlines():
                    for value in re.split(r'[\s,、，；]+', typed_value(line)):
                        if value:
                            item['reference_ids'].append(value)
            elif field == 'id':
                item['id'] = typed_value(text)
            else:
                item[field] = text
            if field not in {'description', 'constraints', 'output_if'}:
                if any(block['type'] == 'table' or any(part['type'] != 'text' for part in block['content']) for block in blocks):
                    self.warn('COMPLEX_FIELD', f'Check the original table for non-text content in {field}.')
            if field in {'constraints', 'output_if'}:
                item[f'{field}_blocks'] = blocks
        if not item['id']:
            self.warn('MISSING_ID', 'Item has no ID.')
        if not item['description']['blocks']:
            self.warn('EMPTY_DESCRIPTION', 'Item has no recognized Description content.')
        if not section:
            self.warn('MISSING_SECTION_NUMBER', 'No explicit section number. Cite the body table index.', review=False)
        return item

    def extract(self):
        root = xml_part(self.package, PART, required=True)
        body = root.find(W + 'body')
        if body is None:
            raise ValueError('DOCX has no supported Word document body')
        if any(node.tag in REVISION_TAGS for node in body.iter()):
            self.warn('TRACKED_CHANGE_PRESENT', 'Tracked changes exist. Inserted text is included and deleted text is excluded where recognized. Check Word before relying on the result.')
        if any(node.tag == W + 'altChunk' for node in body.iter()):
            self.warn('UNSUPPORTED_OBJECT', 'The document contains alternate/imported content (altChunk).')
        items = []
        heading = None
        table_index = 0
        for block in children(body):
            if block.tag == W + 'p':
                parsed_heading = self.heading(block)
                if parsed_heading:
                    heading = parsed_heading
            elif block.tag == W + 'tbl':
                table_index += 1
                self.table_index = table_index
                item = self.item(block, heading)
                if item is not None:
                    items.append(item)
        counts = Counter(item['id'] for item in items if item['id'])
        for item in items:
            self.table_index = item['source']['table_index']
            if counts[item['id']] > 1:
                self.warn('DUPLICATE_ID', f"ID occurs {counts[item['id']]} times: {item['id']}")
        self.table_index = None
        if not items:
            self.warn('NO_ITEMS', f'No specification tables matched among {table_index} body tables.')
        by_table = {}
        for warning in self.warnings:
            if 'table_index' in warning:
                by_table.setdefault(warning['table_index'], []).append(warning)
        for item in items:
            item['warnings'] = by_table.get(item['source']['table_index'], [])
        return items


def markdown_line(line):
    line = line.replace('\\', '\\\\').replace('`', '\\`')
    if re.match(r'^\s*(?:#{1,6}\s|>|[-+*]\s)', line):
        return re.sub(r'^(\s*)(\S)', r'\1\\\2', line, count=1)
    numbered = re.match(r'^(\s*\d+)([.)]\s)', line)
    if numbered:
        return numbered.group(1) + '\\' + numbered.group(2) + line[numbered.end():]
    return line


def markdown_text(text):
    return markdown_line(text)


def render_blocks(blocks, prefix=''):
    for block in blocks:
        if block['type'] == 'table':
            for row_index, row in enumerate(block['rows'], 1):
                for cell in row['cells']:
                    yield f"{prefix}**Row {row_index}, column {cell['column']}**"
                    yield prefix.rstrip()
                    yield from render_blocks(cell['blocks'], prefix + '> ')
            continue
        pieces = []
        for part in block['content']:
            if part['type'] == 'text':
                pieces.append(markdown_text(part['text']))
            elif part['type'] == 'image':
                pieces.append(f"[Image]({part['path']})")
            else:
                pieces.append(f" [Unsupported: {markdown_text(part['object'])}] ")
        lines = ''.join(pieces).splitlines()
        for index, line in enumerate(lines):
            marker = '- ' if block['type'] == 'list_item' and index == 0 else ''
            yield prefix + marker + line
        yield prefix.rstrip()


def render_markdown(items, manifest, warnings):
    lines = ['# Test specifications', '', f"Source: {markdown_text(manifest['source_file'])}",
             f"SHA-256: `{manifest['source_sha256']}`", '']
    for warning in warnings:
        if 'table_index' not in warning:
            lines.extend([f"- {warning['code']}: {markdown_text(warning['message'])}", ''])
    for item in items:
        title = ' '.join(part for part in (item['section_number'], item['title']) if part) or 'Specification item'
        lines.extend([f"## {markdown_text(title)}", '', f"Body table: {item['source']['table_index']}", ''])
        for field, label in (('id', 'ID'), ('reference_id_raw', 'REF'), ('asil', 'ASIL'), ('classification', 'Classification')):
            if item[field]:
                lines.extend([f"**{label}** {markdown_text(item[field])}", ''])
        for field, label in (('constraints_blocks', 'Constraints'), ('output_if_blocks', 'Output I/F')):
            if item[field]:
                lines.extend([f'**{label}**', '', *render_blocks(item[field])])
        lines.extend(render_blocks(item['description']['blocks']))
        for warning in item['warnings']:
            lines.append(f"- {warning['code']}: {markdown_text(warning['message'])}")
        lines.append('')
    return '\n'.join(lines) + '\n'


def json_text(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + '\n'


def extract(source, out):
    if source.suffix.lower() != '.docx':
        raise ValueError('Expected a .docx file')
    if out.exists() or out.is_symlink():
        raise ValueError(f'Output already exists: {out}. Choose a fresh directory.')
    with source.open('rb') as stream:
        data = stream.read(MAX_SOURCE + 1)
    if len(data) > MAX_SOURCE:
        raise ValueError('DOCX exceeds the 64 MiB input limit')
    with ZipFile(io.BytesIO(data)) as package:
        entries = package.infolist()
        if sum(entry.file_size for entry in entries) > MAX_EXPANDED:
            raise ValueError('DOCX exceeds the 256 MiB expanded package limit')
        if len({entry.filename for entry in entries}) != len(entries):
            raise ValueError('DOCX package has duplicate part names')
        extractor = Extractor(package)
        items = extractor.extract()
    warnings = extractor.warnings
    manifest = {'schema_version': SCHEMA_VERSION, 'extractor_version': VERSION,
                'source_file': source.name, 'source_sha256': hashlib.sha256(data).hexdigest(),
                'item_count': len(items), 'image_count': extractor.image_count,
                'unique_asset_count': len(extractor.assets), 'warning_count': len(warnings),
                'warning_counts': dict(sorted(Counter(warning['code'] for warning in warnings).items())),
                'requires_review': any(warning['requires_review'] for warning in warnings)}
    out.mkdir()
    assets = out / 'assets'
    assets.mkdir()
    for filename, content in extractor.assets.items():
        (assets / filename).write_bytes(content)
    (out / 'tests.json').write_text(json_text({'document': manifest, 'test_items': items}), encoding='utf-8')
    (out / 'warnings.json').write_text(json_text(warnings), encoding='utf-8')
    (out / 'tests.md').write_text(render_markdown(items, manifest, warnings), encoding='utf-8')
    (out / 'manifest.json').write_text(json_text(manifest), encoding='utf-8')
    return {'bundle': str(out.resolve()), **manifest}


def load_bundle(bundle):
    if not (bundle / 'manifest.json').is_file():
        raise ValueError('Bundle has no manifest.json. Extract into a fresh directory.')
    data = json.loads((bundle / 'tests.json').read_text(encoding='utf-8'))
    document = data.get('document', {}) if isinstance(data, dict) else {}
    if document.get('schema_version') != SCHEMA_VERSION or not isinstance(data.get('test_items'), list):
        raise ValueError('Expected a version 1 word-test-spec bundle')
    warnings = json.loads((bundle / 'warnings.json').read_text(encoding='utf-8'))
    return data, [warning for warning in warnings if 'table_index' not in warning]


def search(bundle, args):
    data, global_warnings = load_bundle(bundle)
    matches = []
    terms = args.query.casefold().split() if args.query else []
    for item in data['test_items']:
        if args.id is not None and item['id'] != args.id:
            continue
        if args.ref is not None and args.ref not in item['reference_ids']:
            continue
        if args.asil is not None and item['asil'].strip().casefold() != args.asil.casefold():
            continue
        if args.classification is not None and item['classification'].strip().casefold() != args.classification.casefold():
            continue
        searchable = '\n'.join([item['id'], item['reference_id_raw'], item['title'], item['section_number'],
                                item['classification'], item['asil'], item['constraints'], item['output_if'],
                                item['description']['plain_text']]).casefold()
        if not all(term in searchable for term in terms):
            continue
        matches.append({key: item[key] for key in ('id', 'reference_ids', 'asil', 'classification', 'section_number', 'title', 'source')})
        matches[-1]['excerpt'] = ' '.join(item['description']['plain_text'].split())[:160]
        matches[-1]['warning_codes'] = [warning['code'] for warning in item['warnings']]
    return {'document': data['document'], 'document_warnings': global_warnings,
            'total_matches': len(matches), 'matches': matches[:args.limit]}


def get_item(bundle, args):
    data, global_warnings = load_bundle(bundle)
    matches = [item for item in data['test_items'] if (
        item['id'] == args.id if args.id is not None else item['source']['table_index'] == args.table_index)]
    if not matches:
        raise ValueError('No matching item')
    if len(matches) > 1:
        indices = ', '.join(str(item['source']['table_index']) for item in matches)
        raise ValueError(f'Duplicate ID. Use get --table-index with one of: {indices}')
    return {'document': data['document'], 'document_warnings': global_warnings, 'item': matches[0]}


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    extract_parser = commands.add_parser('extract', help='Extract a fresh bundle from a DOCX')
    extract_parser.add_argument('source', type=Path)
    extract_parser.add_argument('--out', type=Path, required=True)
    search_parser = commands.add_parser('search', help='Search an extracted bundle')
    search_parser.add_argument('bundle', type=Path)
    for name in ('query', 'id', 'ref', 'asil', 'classification'):
        search_parser.add_argument(f'--{name}')
    search_parser.add_argument('--limit', type=int, default=20)
    get_parser = commands.add_parser('get', help='Read a full specification item')
    get_parser.add_argument('bundle', type=Path)
    selectors = get_parser.add_mutually_exclusive_group(required=True)
    selectors.add_argument('--id')
    selectors.add_argument('--table-index', type=int)
    args = parser.parse_args()
    try:
        if args.command == 'extract':
            result = extract(args.source, args.out)
        elif args.command == 'search':
            if not 1 <= args.limit <= 100:
                search_parser.error('--limit must be from 1 to 100')
            result = search(args.bundle, args)
        else:
            result = get_item(args.bundle, args)
    except (BadZipFile, ET.ParseError, KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
        parser.exit(1, f'word-test-spec: {exc}\n')
    print(json_text(result), end='')


if __name__ == '__main__':
    main()
