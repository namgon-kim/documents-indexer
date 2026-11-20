"""Map pyhwp's HwpDoc XML into native extraction events.

The XML is an intermediate representation produced by pyhwp, not HWPX.
Locations describe source section/paragraph references, not rendered pages.
"""
from html import escape

from indexer.contracts import IndexerError


def _tag(node):
    return node.tag.rsplit('}', 1)[-1]


def _text(node):
    """Read text belonging to this node, excluding nested tables and shapes."""
    result = []
    def visit(item):
        name = _tag(item)
        if name in {'TableControl', 'GShapeObjectControl', 'SectionDef'}:
            return
        if name == 'Text':
            result.append(item.text or '')
            return
        if name == 'ControlChar' and item.get('name', '').upper() in {'TAB', 'LINE_BREAK'}:
            result.append('\t' if item.get('name', '').upper() == 'TAB' else '\n')
            return
        for child in item:
            visit(child)
    visit(node)
    return ''.join(result).strip()


class HwpStructure:
    def __init__(self, extractor, root, assets):
        self.extractor, self.root, self.assets = extractor, root, assets
        self.used_assets = set()
        mappings = root.find('./DocInfo/IdMappings')
        if mappings is None:
            raise ValueError('missing HWP document mappings')
        self.shapes = mappings.findall('ParaShape')
        self.styles = mappings.findall('Style')
        self.bindata = mappings.findall('BinData')
        self.paragraph_index = 0

    def paragraph_style(self, paragraph):
        value = paragraph.get('parashape-id')
        if value is None:
            style_id = paragraph.get('style-id', '')
            style = self.styles[int(style_id)] if style_id.isdigit() and int(style_id) < len(self.styles) else None
            value = style.get('parashape-id') if style is not None else None
        if value is None or not value.isdigit() or int(value) >= len(self.shapes):
            self.extractor.warning('일부 HWP 문단의 스타일을 확인하지 못했습니다.')
            return None, None, value
        shape = self.shapes[int(value)]
        kind = shape.get('head-shape', 'none').lower()
        raw_level = shape.get('level', '')
        level = min(int(raw_level) + 1, 6) if raw_level.isdigit() and int(raw_level) <= 6 else None
        if kind in {'outline', 'number', 'bullet'}:
            self.extractor.limitation('HWP 자동 개요·목록 번호의 화면 표시 형식은 재현하지 않습니다. 개요 종류와 수준은 보존합니다.')
        if kind == 'outline' and raw_level == '6':
            self.extractor.limitation('7단계 HWP 개요는 검색 제목 경로의 6단계에 표시됩니다. 원본 수준은 보존합니다.')
        if kind in {'outline', 'number', 'bullet'} and level is None:
            self.extractor.warning('일부 HWP 개요 또는 목록 수준을 확인하지 못했습니다.')
        return kind, level, value

    def add_paragraph(self, text, location, container, kind, level, shape_id, style_id):
        if not text:
            return
        heading = kind == 'outline' and level is not None
        category = 'heading' if heading else 'list_item' if kind in {'number', 'bullet'} else 'paragraph'
        self.extractor.add(category, text, f'<h{level}>{escape(text)}</h{level}>' if heading else '',
                           location, container=container)
        item = self.extractor.elements[-1]
        item['_hwp_style'] = {'paragraph_shape_id': shape_id, 'style_id': style_id,
                              'head_shape': kind, 'level': level}
        if shape_id is not None and str(shape_id).isdigit() and int(shape_id) < len(self.shapes):
            item['_hwp_style']['source_level'] = self.shapes[int(shape_id)].get('level')
            item['_hwp_style']['numbering_bullet_id'] = self.shapes[int(shape_id)].get('numbering-bullet-id')
        if heading:
            item['_heading_explicit'] = True

    def image(self, picture, location, container, control=None):
        info = picture.find('.//PictureInfo')
        value = info.get('bindata-id', '') if info is not None else ''
        if not value.isdigit() or not 1 <= int(value) <= len(self.bindata):
            self.extractor.warning('HWP 그림의 원본 참조를 확인하지 못했습니다.')
            return
        entry = self.bindata[int(value) - 1].find('BinDataEmbedding')
        if entry is None:
            self.extractor.warning('HWP 그림이 외부 또는 지원하지 않는 원본을 참조합니다.')
            return
        storage_id, suffix = entry.get('storage-id', ''), entry.get('ext', '')
        key = (storage_id + '.' + suffix).casefold()
        asset = self.assets.get(key)
        if asset is None:
            self.extractor.warning('HWP 그림의 원본 파일을 찾지 못했습니다.')
            return
        self.used_assets.add(key)
        layout = None
        if control is not None:
            attrs = {name: control.get(name) for name in ('x', 'y', 'width', 'height', 'vrelto', 'hrelto', 'inline') if control.get(name) is not None}
            clip = picture.find('ImageClip')
            layout = {'space': 'hwp_object', 'origin': 'source', 'status': 'unverified', **attrs}
            if clip is not None:
                layout['clip'] = dict(clip.attrib)
        self.extractor.image(asset[0], asset[1], location, layout, container)

    def table(self, control, location, container):
        body = control.find('TableBody')
        rows = body.findall('TableRow') if body is not None else []
        if not rows or len(rows) > 5000:
            raise IndexerError('NATIVE_TABLE_LIMIT', 'HWP 표 구조를 확인할 수 없거나 처리 범위를 넘습니다.', 422)
        occupied, rendered, contained = set(), [], []
        for row_no, row in enumerate(rows):
            cells = []
            cursor = 0
            for cell in row.findall('TableCell'):
                try:
                    cell_row = int(cell.get('row', str(row_no)))
                    col = int(cell.get('col', str(cursor)))
                    rowspan, colspan = int(cell.get('rowspan', '1')), int(cell.get('colspan', '1'))
                except ValueError as error:
                    raise ValueError('invalid HWP table cell') from error
                if cell_row != row_no or col < cursor or col >= 100 or not 1 <= rowspan <= len(rows) - row_no or not 1 <= colspan <= 100 - col:
                    raise ValueError('invalid HWP table bounds')
                while cursor < col:
                    if (row_no, cursor) not in occupied:
                        cells.append('<td></td>')
                    cursor += 1
                span = {(r, c) for r in range(row_no, row_no + rowspan) for c in range(col, col + colspan)}
                if span & occupied or len(occupied) + len(span) > 25000:
                    raise ValueError('overlapping HWP table cells')
                occupied.update(span)
                paragraphs = [_text(p) for p in cell.findall('Paragraph')]
                value = '\n'.join(text for text in paragraphs if text)
                cells.append(f'<td rowspan="{rowspan}" colspan="{colspan}">{escape(value)}</td>')
                cursor = col + colspan
                def collect(node):
                    if _tag(node) in {'ShapePicture', 'GShapeObjectControl', 'TableControl'}:
                        contained.append(node)
                        return
                    for child in node:
                        collect(child)
                for child in cell:
                    collect(child)
            rendered.append('<tr>' + ''.join(cells) + '</tr>')
        captions = [text for caption in control.findall('TableCaption') if (text := _text(caption))]
        caption_html = '<caption>' + escape('\n'.join(captions)) + '</caption>' if captions else ''
        self.extractor.add('table', html='<table>' + caption_html + ''.join(rendered) + '</table>', location=location, container=container)
        for nested in contained:
            if _tag(nested) == 'ShapePicture':
                self.image(nested, location, container)
            elif _tag(nested) == 'GShapeObjectControl':
                self.shape(nested, location, container)
            else:
                from indexer.review_status import HWP_NESTED_TABLE_LIMITATION
                self.extractor.limitation(HWP_NESTED_TABLE_LIMITATION)
                self.table(nested, location, container)

    def shape(self, control, location, container):
        pictures, nested_paragraphs = [], []
        def collect(node):
            if _tag(node) == 'Paragraph':
                nested_paragraphs.append(node)
                return
            if _tag(node) == 'ShapePicture':
                pictures.append(node)
                return
            for child in node:
                collect(child)
        collect(control)
        for picture in pictures:
            self.image(picture, location, container, control)
        for paragraph in nested_paragraphs:
            self.paragraph(paragraph, location['section'])
        if not pictures and not nested_paragraphs:
            self.extractor.warning('HWP의 일부 도형·차트 관계는 확인하지 못했습니다.')

    def paragraph(self, paragraph, section):
        index = self.paragraph_index
        self.paragraph_index += 1
        location = {'kind': 'hwp_paragraph', 'section': section, 'paragraph': index,
                    'source_paragraph_id': paragraph.get('paragraph-id')}
        container = f'hwp-section-{section}-paragraph-{index}'
        kind, level, shape_id = self.paragraph_style(paragraph)
        style_id = paragraph.get('style-id')
        segments = []
        has_heading = False
        def flush():
            nonlocal has_heading
            text = ''.join(segments).strip()
            segments.clear()
            if text:
                self.add_paragraph(text, location, container, 'none' if has_heading else kind, level, shape_id, style_id)
                if kind == 'outline':
                    has_heading = True
        for line in paragraph.findall('LineSeg'):
            for child in line:
                tag = _tag(child)
                if tag == 'Text':
                    segments.append(child.text or '')
                elif tag == 'ControlChar' and child.get('name', '').upper() in {'TAB', 'LINE_BREAK'}:
                    segments.append('\t' if child.get('name', '').upper() == 'TAB' else '\n')
                elif tag == 'TableControl':
                    flush()
                    self.table(child, location, container)
                elif tag == 'GShapeObjectControl':
                    flush()
                    self.shape(child, location, container)
                elif tag not in {'ColumnsDef', 'AutoNumbering'}:
                    nested = _text(child)
                    if nested:
                        segments.append(nested)
            if segments and not segments[-1].endswith((' ', '\n', '\t')):
                segments.append(' ')
        flush()

    def extract(self):
        body = self.root.find('BodyText')
        if body is None:
            raise ValueError('missing HWP body')
        if next(body.iter('AutoNumbering'), None) is not None:
            self.extractor.limitation('HWP 자동 번호의 화면 표시 형식은 재현하지 않습니다.')
        sections = body.findall('SectionDef')
        if not sections:
            raise ValueError('missing HWP sections')
        for fallback, section in enumerate(sections):
            value = section.get('section-id', '')
            section_id = int(value) if value.isdigit() else fallback
            def walk(node, section_id=section_id):
                if _tag(node) == 'Paragraph':
                    self.paragraph(node, section_id)
                    return
                for child in node:
                    walk(child)
            for child in section:
                if _tag(child) in {'ColumnSet', 'Paragraph'}:
                    walk(child)
                elif next(child.iter('Paragraph'), None) is not None:
                    self.extractor.warning('HWP의 일부 구역 내용은 구조를 확인하지 못했습니다.')
        for key, asset in self.assets.items():
            if key not in self.used_assets:
                self.extractor.image(asset[0], asset[1], None)
                self.extractor.warning('본문과의 위치를 확인하지 못한 HWP 이미지가 있습니다.')
