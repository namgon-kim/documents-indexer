"""Direct Office extraction with explicit, separately attributed image analysis."""

import hashlib
import io
import json
import logging
import re
import subprocess
import sys
import tempfile
import warnings
import zlib
from html import escape
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import httpx
import olefile
from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from PIL import Image, UnidentifiedImageError
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE, PP_PLACEHOLDER

from indexer.contracts import IndexerError
from indexer.parsing.contracts import ParseRequest, ParseResult

NATIVE_FORMATS = frozenset({'xlsx', 'hwpx', 'hwp', 'pptx', 'docx'})
IMAGE_FORMATS = {'png', 'jpg', 'jpeg'}
CONVERTIBLE_IMAGE_FORMATS = {'bmp', 'gif', 'webp', 'tif', 'tiff'}
LOG = logging.getLogger(__name__)


def _local(tag):
    return tag.rsplit('}', 1)[-1]


def image_for_analysis(data, suffix):
    """Convert a single raster to PNG for OCR without changing the stored original."""
    if suffix in IMAGE_FORMATS:
        return data, suffix
    if suffix not in CONVERTIBLE_IMAGE_FORMATS:
        return None
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > 40_000_000 or getattr(image, 'n_frames', 1) != 1:
                return None
            rgba = image.convert('RGBA')
            background = Image.new('RGB', rgba.size, 'white')
            background.paste(rgba, mask=rgba.getchannel('A'))
            output = io.BytesIO()
            background.save(output, format='PNG')
            if output.tell() > 20 * 1024 * 1024:
                return None
            return output.getvalue(), 'png'
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        return None


class NativeExtractor:
    def __init__(self, parser, mode, checkpoint=None, persist=None, progress=None, save_image=None, *, xlsx_analyzer=None, xlsx_review=None):
        self.parser, self.mode = parser, mode
        self.checkpoint, self.persist, self.progress = checkpoint or {}, persist, progress
        self.save_image = save_image
        self.elements, self.warnings, self.limitations, self.visuals = [], [], [], []
        self.xlsx_sheets = []
        self.xlsx_metadata = {}
        self.xlsx_notices = []
        self.xlsx_analyzer, self.xlsx_review = xlsx_analyzer, xlsx_review or {}

    def add(self, kind, text='', html='', location=None, layout=None, container=None):
        if text or html:
            self.elements.append({'id': len(self.elements), 'page': None, 'category': kind,
                                  'content': {'text': str(text), 'html': html},
                                  'source_location': location, '_origin': 'native_extraction',
                                  '_layout': layout, '_container': container})

    def warning(self, message):
        if message not in self.warnings:
            self.warnings.append(message)

    def limitation(self, message):
        if message not in self.limitations:
            self.limitations.append(message)

    @staticmethod
    def _docx_heading_level(paragraph):
        def outline(element):
            if element is None:
                return None
            nodes = element.xpath('./*[local-name()="outlineLvl"]')
            value = nodes[0].get('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val') if nodes else None
            if value and value.isdigit():
                return min(int(value) + 1, 6) if int(value) < 9 else 0
            return None
        direct = outline(paragraph._p.pPr)
        if direct is not None:
            return direct or None
        style, seen = paragraph.style, set()
        while style is not None and style.style_id not in seen:
            seen.add(style.style_id)
            match = re.fullmatch(r'heading\s*([1-9])', style.style_id or '', re.IGNORECASE)
            level = min(int(match.group(1)), 6) if match else outline(style._element.pPr)
            if level is not None:
                return level or None
            style = style.base_style
        return None

    @staticmethod
    def _numbered_heading_level(text):
        # A dotted decimal followed by text is ambiguous with a measurement.
        # Infer only markers with a terminating period; keep other lines as text.
        match = re.match(r'^(\d{1,2}(?:\.\d{1,3})*)\.\s+(\S.*)$', text.strip())
        if match and len(text) <= 100 and not re.search(r'[。.!?]$', text):
            return min(match.group(1).count('.') + 1, 6)
        return None

    @classmethod
    def _consecutive_numbered_items(cls, paragraphs):
        """Adjacent numbered paragraphs are more likely a list than section headings."""
        numbered = [bool(cls._numbered_heading_level(value)) if value else False for value in paragraphs]
        return {index for index, is_numbered in enumerate(numbered) if is_numbered and
                ((index > 0 and numbered[index - 1]) or (index + 1 < len(numbered) and numbered[index + 1]))}

    @staticmethod
    def _docx_is_list(paragraph):
        if paragraph._p.xpath('./*[local-name()="pPr"]/*[local-name()="numPr"]'):
            return True
        style, seen = paragraph.style, set()
        while style is not None and style.style_id not in seen:
            seen.add(style.style_id)
            if style._element.xpath('./*[local-name()="pPr"]/*[local-name()="numPr"]'):
                return True
            style = style.base_style
        return False

    def image(self, data, suffix, location, layout=None, container=None, *, preview=False):
        suffix = suffix.lower().lstrip('.')
        if len(self.visuals) >= 100:
            raise IndexerError('NATIVE_IMAGE_LIMIT', '이미지가 100개를 넘어 직접 분석할 수 없습니다.', 413)
        if len(data) > 20 * 1024 * 1024 or sum(len(item[0]) for item in self.visuals) + len(data) > 100 * 1024 * 1024:
            raise IndexerError('NATIVE_IMAGE_LIMIT', '이미지 크기가 직접 처리 범위를 넘습니다.', 413)
        asset_id = hashlib.sha256(data).hexdigest()
        if self.save_image:
            self.save_image(asset_id, data)
        self.elements.append({'_native_image_index': len(self.visuals), 'asset_id': asset_id,
                              'suffix': suffix, 'source_location': location, '_layout': layout, '_container': container,
                              '_image_is_preview': preview})
        self.visuals.append((data, suffix, location))

    def extract(self, path, kind):
        if kind == 'xlsx':
            self._xlsx(path)
        elif kind == 'hwpx':
            self._hwpx(path)
        elif kind == 'hwp':
            self._hwp(path)
        elif kind == 'pptx':
            self._pptx(path)
        elif kind == 'docx':
            self._docx(path)
        else:
            raise IndexerError('NATIVE_FORMAT_UNSUPPORTED', '이 문서 형식은 직접 추출을 지원하지 않습니다.', 415)
        visual_count = len(self.visuals)
        analyzed = {}
        if self.mode == 'native_text' and visual_count:
            self.warning(f'이미지 {visual_count}개는 분석하지 않았습니다.')
        elif self.mode == 'native_visual':
            if visual_count and getattr(self.parser, 'config', None) is None:
                raise IndexerError('VISUAL_PARSER_UNSUPPORTED', '선택한 파서는 이미지 분석을 지원하지 않습니다.', 422)
            if visual_count and getattr(self.parser.config, 'provider', '') != 'paddle_vl':
                raise IndexerError('VISUAL_PARSER_UNSUPPORTED', '이미지 분석은 현재 PaddleOCR-VL 파서에서 지원합니다.', 422)
            if visual_count and self.progress:
                self.progress(0, visual_count)
            unknown_previous = any(item.get('status') != 'completed' for item in self.checkpoint.get('visuals', []))
            for image_index, (data, suffix, location) in enumerate(self.visuals):
                prepared = image_for_analysis(data, suffix)
                if prepared is None:
                    self.warning('지원하지 않는 형식이거나 여러 프레임·너무 큰 이미지가 있어 분석하지 못했습니다.')
                    if self.progress:
                        self.progress(image_index + 1, visual_count)
                    continue
                analysis_data, analysis_suffix = prepared
                digest = hashlib.sha256(data).hexdigest()
                saved = next((item for item in self.checkpoint.get('visuals', []) if item.get('index') == image_index), None)
                if saved and saved.get('sha256') != digest:
                    raise IndexerError('VISUAL_CHECKPOINT_CHANGED', '저장된 이미지 처리 대상이 변경되었습니다.', 409)
                if saved and saved.get('status') != 'completed':
                    self.warning('이전 이미지 요청의 완료 여부를 확인할 수 없어 추가 분석을 중단했습니다. 이미지 목록에서 처리 방식을 선택해 주세요.')
                    continue
                if unknown_previous and not saved:
                    self.warning('이전 이미지 요청의 완료 여부를 확인할 수 없어 추가 분석을 중단했습니다. 이미지 목록에서 처리 방식을 선택해 주세요.')
                    continue
                if saved:
                    result = ParseResult('paddle_vl', saved.get('elements', []), {})
                else:
                    if self.persist:
                        self.persist('visual', {'index': image_index, 'sha256': digest, 'status': 'started'})
                    try:
                        with tempfile.TemporaryDirectory() as folder:
                            image = Path(folder) / ('image.' + analysis_suffix)
                            image.write_bytes(analysis_data)
                            result = self.parser.parse(ParseRequest(image, analysis_suffix, page_count=1,
                                timeout_seconds=getattr(self.parser, 'image_timeout_seconds', None)))
                    except (IndexerError, httpx.HTTPError) as error:
                        self.warning('이미지 분석 응답을 확인하지 못해 추가 분석을 중단했습니다. 이미지 목록에서 처리 방식을 선택해 주세요.')
                        LOG.warning(json.dumps({'event': 'visual_analysis_stopped', 'image_index': image_index,
                                                'error_code': getattr(error, 'code', type(error).__name__)}))
                        break
                    if self.persist and not result.incomplete and not result.unavailable:
                        self.persist('visual', {'index': image_index, 'sha256': digest,
                                                'status': 'completed', 'elements': result.elements})
                if self.progress:
                    self.progress(image_index + 1, visual_count)
                if result.incomplete or result.unavailable or not result.elements:
                    self.warning('결과를 확인할 수 없는 이미지가 있습니다.')
                    continue
                analyzed[image_index] = []
                for item in result.elements:
                    content = item.get('content') or {}
                    if content.get('text') or content.get('html'):
                        category = item.get('category', 'figure')
                        if category == 'heading':
                            # An image's internal title must not become the document section heading.
                            category = 'paragraph'
                            content = {**content, 'html': ''}
                        analyzed[image_index].append({'id': f'visual-{image_index}', 'page': None,
                            'category': category, 'content': content,
                            'source_location': location, '_origin': 'visual_analysis',
                            '_asset_id': digest})
                if not analyzed[image_index]:
                    self.warning('텍스트를 확인할 수 없는 이미지가 있습니다.')
        output = []
        for element in self.elements:
            if '_native_image_index' not in element:
                output.append(element)
                continue
            index = element['_native_image_index']
            output.append({'id': f'image-{index}', 'page': None, 'category': 'image',
                           'content': {'text': '', 'html': ''}, 'source_location': element['source_location'],
                           '_origin': 'native_image', '_asset_id': element['asset_id'],
                           '_visual_id': f'image-{index}', '_image_format': element['suffix'],
                           '_layout': element['_layout'], '_container': element['_container'],
                           '_image_is_preview': element.get('_image_is_preview', False)})
            for item in analyzed.get(index, []):
                item['_layout'], item['_container'] = element['_layout'], element['_container']
                item['_visual_id'] = f'image-{index}'
                output.append(item)
        self.elements = output
        if not self.elements:
            raise IndexerError('NATIVE_EXTRACTION_EMPTY', '직접 추출한 검색 내용이 없습니다. 다른 처리 방식을 선택해 주세요.', 422)
        self.visuals.clear()
        return ParseResult('native_' + kind, self.elements,
                           {'provider': 'native_' + kind, 'mode': self.mode,
                            'warnings': self.warnings, 'limitations': self.limitations, 'visual_count': visual_count,
                            **({'xlsx_sheets': self.xlsx_sheets, 'xlsx_notices': self.xlsx_notices, **self.xlsx_metadata} if kind == 'xlsx' else {})},
                           incomplete=any(message not in self.xlsx_notices for message in self.warnings))

    def _docx(self, path):
        self.limitation('DOCX 직접 추출은 페이지 배치, 머리글·바닥글과 도형 연결을 보장하지 않습니다.')
        try:
            document = Document(path)
            embed_attr = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed'
            blocks = list(document.iter_inner_content())
            has_headings = any(isinstance(block, Paragraph) and self._docx_heading_level(block) for block in blocks)
            list_indices = self._consecutive_numbered_items([block.text if isinstance(block, Paragraph) else '' for block in blocks])
            for index, block in enumerate(blocks, 1):
                location = {'kind': 'docx_block', 'block': index}
                container = f'docx-block-{index}'
                if isinstance(block, Paragraph):
                    level = self._docx_heading_level(block)
                    list_item = self._docx_is_list(block)
                    inferred_level = self._numbered_heading_level(block.text) if not has_headings and not list_item and index - 1 not in list_indices else None
                    inferred = not level and bool(inferred_level)
                    level = level or inferred_level
                    self.add('heading' if level else 'paragraph', block.text,
                             f'<h{level}>{escape(block.text)}</h{level}>' if level else '', location=location, container=container)
                    if inferred and block.text:
                        self.elements[-1]['_heading_origin'] = 'inferred'
                elif isinstance(block, Table):
                    rows = []
                    if len(block.rows) > 5000 or len(block.columns) > 100 or len(block.rows) * len(block.columns) > 25000:
                        raise IndexerError('NATIVE_TABLE_LIMIT', 'DOCX 표가 직접 추출 범위를 넘습니다.', 413)
                    # The Word layout grid may repeat merged cells; do not invent spans.
                    for row in block.rows:
                        header = bool(row._tr.xpath('./*[local-name()="trPr"]/*[local-name()="tblHeader" and not(@*[local-name()="val"]="0" or @*[local-name()="val"]="false")]'))
                        tag = 'th' if header else 'td'
                        rows.append('<tr>' + ''.join(f'<{tag}>' + escape(cell.text) + f'</{tag}>' for cell in row.cells) + '</tr>')
                    self.add('table', html='<table>' + ''.join(rows) + '</table>', location=location, container=container)
                    if block._element.xpath('.//*[local-name()="gridSpan" or local-name()="vMerge"]'):
                        self.warning('DOCX 표의 병합 셀 관계는 확인이 필요합니다.')
                for node in block._element.xpath('.//*[local-name()="blip"]'):
                    rel = node.get(embed_attr)
                    part = document.part.related_parts.get(rel) if rel else None
                    if part is None or not part.content_type.startswith('image/'):
                        self.warning('DOCX 이미지 참조를 확인하지 못했습니다.')
                        continue
                    self.image(part.blob, Path(str(part.partname)).suffix, location, container=container)
        except IndexerError:
            raise
        except Exception as error:
            raise IndexerError('NATIVE_EXTRACTION_FAILED', 'DOCX 본문 또는 이미지를 읽을 수 없습니다.', 422) from error

    def _pptx(self, path):
        self.limitation('PPTX 직접 추출은 도형의 배치·연결 관계와 발표자 노트를 보장하지 않습니다.')
        try:
            deck = Presentation(path)
            for slide_number, slide in enumerate(deck.slides, 1):
                location = {'kind': 'pptx_slide', 'slide': slide_number}
                self.add('heading', f'슬라이드 {slide_number}', f'<h1>슬라이드 {slide_number}</h1>', location)

                def shapes(items, location=location, grouped=False):
                    for shape in items:
                        container = f"pptx-slide-{location['slide']}-shape-{shape.shape_id}"
                        bounds = (shape.left, shape.top, shape.width, shape.height)
                        layout = None
                        if not grouped and not shape.rotation and all(value is not None for value in bounds):
                            layout = {'space': 'pptx_emu', 'left': int(bounds[0]), 'top': int(bounds[1]),
                                      'width': int(bounds[2]), 'height': int(bounds[3]),
                                      'container_width': int(deck.slide_width), 'container_height': int(deck.slide_height)}
                        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
                            shapes(shape.shapes, location, True)
                        elif shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                            self.image(shape.image.blob, shape.image.ext, location, layout)
                        elif shape.has_table:
                            table = shape.table
                            if len(table.rows) > 5000 or len(table.columns) > 100 or len(table.rows) * len(table.columns) > 25000:
                                raise IndexerError('NATIVE_TABLE_LIMIT', '슬라이드 표가 직접 추출 범위를 넘습니다.', 413)
                            rows = []
                            for row in table.rows:
                                cells = []
                                for cell in row.cells:
                                    if cell.is_spanned:
                                        continue
                                    attrs = f' rowspan="{cell.span_height}" colspan="{cell.span_width}"' if cell.is_merge_origin else ''
                                    cells.append(f'<td{attrs}>{escape(cell.text)}</td>')
                                rows.append('<tr>' + ''.join(cells) + '</tr>')
                            self.add('table', html='<table>' + ''.join(rows) + '</table>', location=location, layout=layout)
                        elif shape.has_text_frame:
                            is_title = shape.is_placeholder and shape.placeholder_format.type in {PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.CENTER_TITLE}
                            for paragraph in shape.text_frame.paragraphs:
                                text = paragraph.text
                                self.add('heading' if is_title else 'paragraph', text,
                                         f'<h2>{escape(text)}</h2>' if is_title and text else '',
                                         location=location, layout=layout, container=container)
                        elif shape.shape_type != MSO_SHAPE_TYPE.PLACEHOLDER:
                            self.warning('PPTX에 추출하지 못한 도형 또는 차트가 있습니다.')
                shapes(slide.shapes)
        except IndexerError:
            raise
        except Exception as error:
            raise IndexerError('NATIVE_EXTRACTION_FAILED', 'PPTX 슬라이드를 읽을 수 없습니다.', 422) from error

    def _hwp(self, path):
        from indexer.parsing.hwp_structure import HwpStructure

        self.limitation('HWP 원본 구역과 문단을 보존하지만 편집기의 실제 쪽 배치와 도형 연결 의미는 확인하지 못합니다.')
        try:
            assets = {}
            with olefile.OleFileIO(str(path)) as doc:
                for name in doc.listdir():
                    if not name or name[0].lower() != 'bindata':
                        continue
                    if doc.get_size(name) > 20 * 1024 * 1024:
                        self.warning('너무 큰 HWP 첨부 요소는 분석하지 않았습니다.')
                        continue
                    data = doc.openstream(name).read()
                    def image_format(value):
                        return ('png' if value.startswith(b'\x89PNG\r\n\x1a\n') else
                                'jpg' if value.startswith(b'\xff\xd8\xff') else
                                'gif' if value.startswith((b'GIF87a', b'GIF89a')) else
                                'bmp' if value.startswith(b'BM') else
                                'tiff' if value.startswith((b'II*\x00', b'MM\x00*')) else
                                'webp' if value.startswith(b'RIFF') and value[8:12] == b'WEBP' else None)
                    if not image_format(data):
                        try:
                            data = zlib.decompressobj(-15).decompress(data, 20 * 1024 * 1024 + 1)
                        except zlib.error:
                            pass
                    if len(data) > 20 * 1024 * 1024:
                        self.warning('너무 큰 HWP 첨부 요소는 분석하지 않았습니다.')
                        continue
                    suffix = image_format(data)
                    if suffix:
                        if len(assets) >= 100 or sum(len(asset[0]) for asset in assets.values()) + len(data) > 100 * 1024 * 1024:
                            raise IndexerError('NATIVE_IMAGE_LIMIT', 'HWP 이미지 수 또는 크기가 직접 처리 범위를 넘습니다.', 413)
                        assets[name[-1].casefold()] = (data, suffix)
                    else:
                        self.warning('형식을 확인할 수 없는 HWP 첨부 요소는 분석하지 않았습니다.')
            with tempfile.TemporaryDirectory() as folder:
                output = Path(folder) / 'structure.xml'
                command = [str(Path(sys.executable).parent / 'hwp5proc'), 'xml', '--no-validate-wellformed', '--output', str(output), str(path)]
                process = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                         timeout=120, check=False, env={'PATH': '/usr/bin:/bin'})
                if process.returncode or not output.is_file() or output.stat().st_size > 100 * 1024 * 1024:
                    raise ValueError('HWP structural extraction failed')
                with output.open('rb') as xml_file:
                    prefix = xml_file.read(65536).upper()
                    if b'<!DOCTYPE' in prefix or b'<!ENTITY' in prefix:
                        raise ValueError('unsupported HWP XML declaration')
                HwpStructure(self, ET.parse(output).getroot(), assets).extract()
        except IndexerError:
            raise
        except (OSError, ValueError, UnicodeError, ET.ParseError, subprocess.TimeoutExpired) as error:
            raise IndexerError('NATIVE_EXTRACTION_FAILED', 'HWP 구조를 읽지 못했거나 처리 시간이 초과되었습니다.', 422) from error

    def _xlsx(self, path):
        try:
            from contextlib import ExitStack

            from indexer.office_inspection import inspect_office
            from indexer.parsing.xlsx_source import sheet_record, workbook_styles
            from indexer.xlsx_structure import (
                STRUCTURE_VERSION,
                SheetProposal,
                local_structure,
                occupied,
                row_candidates,
                validate,
            )
            source = inspect_office(path, native_cells=True)
            inspected = {item['name']: item for item in source['sheets']}
            if any(not item['formula_scan_complete'] for item in inspected.values()):
                raise IndexerError('NATIVE_TABLE_LIMIT', '시트 원본 구조가 직접 추출 크기 한도를 넘습니다.', 413)
            review = {name: SheetProposal.model_validate(item) for name, item in self.xlsx_review.items()}
            requested = 0
            with ExitStack() as workbooks:
                with warnings.catch_warnings(record=True) as detected:
                    warnings.simplefilter('always')
                    book = load_workbook(path, data_only=False, read_only=False, keep_links=False)
                    workbooks.callback(book.close)
                    values = load_workbook(path, data_only=True, read_only=False, keep_links=False)
                    workbooks.callback(values.close)
                if detected:
                    self.warning('Excel의 일부 확장 기능 또는 시각 요소를 읽지 못했을 수 있습니다.')
                if review.keys() - set(book.sheetnames):
                    raise IndexerError('XLSX_REVIEW_INVALID', '검토할 시트를 찾을 수 없습니다.')
                for sheet in book:
                    source_sheet = inspected[sheet.title]
                    worksheet = source_sheet.get('sheet_kind') != 'chart'
                    sheet_data = sheet_record(book, sheet, source_sheet, values[sheet.title] if worksheet else None)
                    _, _, max_col, max_row = sheet_data['bounds']
                    if max_row > 5000 or max_col > 100 or max_row * max_col > 25000:
                        raise IndexerError('NATIVE_TABLE_LIMIT', '시트 크기가 직접 추출 범위를 넘습니다. 다른 파서를 사용해 주세요.', 413)
                    regions, candidates = local_structure(sheet_data)
                    sheet_data.update(regions=regions, boundary_candidates=candidates, excluded={})
                    if sheet.title in review:
                        try:
                            sheet_data['regions'] = validate(sheet_data, review[sheet.title], 'user')
                        except ValueError as error:
                            raise IndexerError('XLSX_REVIEW_INVALID', str(error)) from error
                        sheet_data['excluded'] = review[sheet.title].excluded
                        sheet_data['analysis_status'] = 'reviewed'
                    elif self.xlsx_analyzer and any(region['status'] == 'unverified' for region in regions):
                        if requested < self.xlsx_analyzer.config.xlsx_structure_max_sheets:
                            try:
                                analyzed, status = self.xlsx_analyzer.analyze(sheet_data)
                            except OSError:
                                analyzed, status = None, 'cache_unavailable'
                            if status in {'analyzed', 'analyzed_uncached', 'provider_error', 'invalid_response', 'incomplete'}:
                                requested += 1
                            sheet_data['analysis_status'] = status
                            if analyzed is not None:
                                proposals, excluded = analyzed
                                if excluded:
                                    # A model can point out cells to review. Keep their search evidence
                                    # until a user accepts the exclusions.
                                    sheet_data.update(analysis_status='review_required',
                                                      suggested_review={'regions': proposals, 'excluded': excluded})
                                else:
                                    sheet_data['regions'] = proposals
                        else:
                            sheet_data['analysis_status'] = 'sheet_limit'
                    self.xlsx_sheets.append(sheet_data)
                    notices = []
                    if sheet_data['hidden_merged_cells']:
                        notices.append(f'{sheet.title} 시트의 병합 영역 안에 숨은 값이 있습니다. 원본을 확인해 주세요.')
                    if any(cell['cache_status'] == 'missing' for cell in occupied(sheet_data).values()):
                        notices.append('저장된 계산 결과가 없는 수식이 있습니다.')
                    if sheet.sheet_state != 'visible':
                        notices.append('숨겨진 시트가 포함되어 있습니다.')
                    for message in notices:
                        if message not in self.xlsx_notices:
                            self.xlsx_notices.append(message)
                            self.warning(message)
                    self.add('heading', sheet.title, '<h1>' + escape(sheet.title) + '</h1>')
                    self.elements[-1]['_xlsx_sheet'] = sheet.title
                    self._xlsx_regions(sheet_data)
                    if sheet._charts:
                        from indexer.parsing.xlsx_chart_preview import chart_preview, renderer_status
                        for chart_index, chart in enumerate(sheet._charts):
                            anchor = getattr(chart.anchor, '_from', None)
                            address = f'{get_column_letter(anchor.col + 1)}{anchor.row + 1}' if anchor else None
                            location = {'kind': 'xlsx_cell', 'sheet': sheet.title, 'cell': address} if address else None
                            layout = ({'space': 'xlsx_anchor', 'sheet': sheet.title, 'start_row': anchor.row + 1,
                                       'start_column': anchor.col + 1} if anchor else None)
                            chart_record = sheet_data['charts'][chart_index]
                            if chart_record['title']:
                                self.add('chart', chart_record['title'], location=location, layout=layout)
                            preview = chart_preview(path, sheet.title, chart_index)
                            if preview:
                                self.image(preview, 'png', location, layout, preview=True)
                                self.limitation('Excel 차트는 원본 시트에서 생성한 대략적인 미리보기로, 실제 표시와 다를 수 있습니다.')
                            else:
                                office, rasterizer = renderer_status()
                                missing = ', '.join(name for name, command in [('LibreOffice', office), ('Poppler(pdftoppm)', rasterizer)] if not command)
                                reason = f'서버에 {missing} 설치 또는 실행 경로 설정이 필요합니다.' if missing else '서버 로그에서 렌더링 실패 원인을 확인해 주세요.'
                                self.warning(f'Excel 차트 미리보기를 만들지 못했습니다: {sheet.title} {address or "위치 미확인"}. {reason}')
                    for image in getattr(sheet, '_images', []):
                        anchor = getattr(image.anchor, '_from', None)
                        end = getattr(image.anchor, 'to', None)
                        address = f'{get_column_letter(anchor.col + 1)}{anchor.row + 1}' if anchor else None
                        location = {'kind': 'xlsx_cell', 'sheet': sheet.title, 'cell': address} if address else None
                        layout = ({'space': 'xlsx_anchor', 'sheet': sheet.title,
                                   'start_row': anchor.row + 1, 'start_column': anchor.col + 1,
                                   **({'end_row': end.row + 1, 'end_column': end.col + 1} if end else {})} if anchor else None)
                        self.image(image._data(), image.format, location, layout)
                relations, complete = row_candidates(self.xlsx_sheets)
                self.xlsx_metadata = {'xlsx_structure_version': STRUCTURE_VERSION,
                                      'xlsx_style_model': workbook_styles(book, self.xlsx_sheets),
                                      'xlsx_row_relations': relations, 'xlsx_row_relations_complete': complete}
        except IndexerError:
            raise
        except Exception as error:
            raise IndexerError('NATIVE_EXTRACTION_FAILED', 'Excel 내용 또는 삽입 이미지를 읽을 수 없습니다.', 422) from error

    def _xlsx_regions(self, sheet):
        from openpyxl.utils.cell import coordinate_to_tuple, range_boundaries

        from indexer.parsing.xlsx_source import search_value
        from indexer.xlsx_structure import address, contains, occupied

        cells = occupied(sheet)
        positions = {key: coordinate_to_tuple(key) for key in cells}
        merge_boxes = [range_boundaries(ref) for ref in sheet['merged_ranges']]
        for region in sheet['regions']:
            area = range_boundaries(f"{region['start']}:{region['end']}")
            left, top, right, bottom = area
            selected = [cell for key, cell in cells.items() if key not in sheet['excluded'] and contains(area, positions[key])]
            by_address = {cell['address']: cell for cell in selected}
            local_merges = {(y, x): (w, h) for x, y, w, h in merge_boxes
                            if contains(area, (y, x)) and contains(area, (h, w))}
            covered = {(row, col) for (y, x), (w, h) in local_merges.items()
                       for row in range(y, h + 1) for col in range(x, w + 1) if (row, col) != (y, x)}
            rows = []
            for row in range(top, bottom + 1):
                parts = []
                for col in range(left, right + 1):
                    if (row, col) in covered:
                        continue
                    key = address(row, col)
                    cell = by_address.get(key)
                    text = search_value(cell) if cell else ''
                    span = local_merges.get((row, col))
                    attrs = f' rowspan="{span[1] - row + 1}" colspan="{span[0] - col + 1}"' if span else ''
                    tag = 'th' if key in region['headers'] else 'td'
                    if tag == 'th':
                        attrs += ' scope="col"'
                    parts.append(f'<{tag}{attrs}>{escape(text)}</{tag}>')
                rows.append('<tr>' + ''.join(parts) + '</tr>')
            location = {'kind': 'xlsx_range', 'sheet': sheet['name'], 'start': region['start'], 'end': region['end']}
            fragments = [{'row': positions[cell['address']][0], 'column': positions[cell['address']][1],
                          'text': search_value(cell), 'address': cell['address'],
                          'hidden_merged': cell['hidden_merged']} for cell in selected]
            title = cells[region['title']]['display_text'] if region['title'] else None
            self.add('table' if region['kind'] in {'table', 'unknown'} else region['kind'],
                     text='\n'.join(part['text'] for part in fragments),
                     html='<table>' + ''.join(rows) + '</table>', location=location,
                     layout={'space': 'xlsx_region', 'row_offset': top - 1, 'column_offset': left - 1,
                             'boundary_origin': region['origin'], 'boundary_status': region['status']})
            self.elements[-1]['_xlsx_region'] = {**region, 'sheet': sheet['name'], 'title_text': title,
                                                 'cells': fragments}

    def _hwpx(self, path):
        self.limitation('HWPX 직접 추출은 쪽 배치, 일부 스타일과 도형 관계를 확인하지 못합니다.')
        try:
            with ZipFile(path) as archive:
                manifest = ET.fromstring(archive.read('Contents/content.hpf'))
                entries = {node.get('id'): node.get('href') for node in manifest.iter()
                           if _local(node.tag) == 'item' and node.get('id') and node.get('href')}
                outline_levels = {}
                list_properties = set()
                if 'Contents/header.xml' in archive.namelist():
                    header = ET.fromstring(archive.read('Contents/header.xml'))
                    for node in header.iter():
                        if _local(node.tag) != 'paraPr' or node.get('id') is None:
                            continue
                        heading = next((child for child in node if _local(child.tag) == 'heading'), None)
                        if heading is not None and heading.get('type', '').upper() in {'NUMBER', 'BULLET'}:
                            list_properties.add(node.get('id'))
                        if heading is not None and heading.get('type', '').upper() == 'OUTLINE':
                            level = heading.get('level', '')
                            if level.isdigit() and int(level) < 9:
                                outline_levels[node.get('id')] = min(int(level) + 1, 6)
                    for node in header.iter():
                        if _local(node.tag) == 'style' and node.get('id') and node.get('paraPrIDRef') in outline_levels:
                            outline_levels['style:' + node.get('id')] = outline_levels[node.get('paraPrIDRef')]
                        if _local(node.tag) == 'style' and node.get('id') and node.get('paraPrIDRef') in list_properties:
                            list_properties.add('style:' + node.get('id'))
                sections = sorted((name for name in archive.namelist() if re.fullmatch(r'Contents/section\d+\.xml', name)),
                                  key=lambda name: int(re.search(r'(\d+)\.xml$', name).group(1)))
                roots = [(section, ET.fromstring(archive.read(section))) for section in sections]
                has_headings = any(_local(node.tag) == 'p' and (node.get('paraPrIDRef') in outline_levels
                                   or 'style:' + node.get('styleIDRef', '') in outline_levels)
                                   for _, root in roots for node in root.iter())
                for section, root in roots:
                    location = {'kind': 'hwpx_section', 'section': section}
                    self._hwpx_walk(root, archive, entries, location, outline_levels, list_properties, infer_numbered=not has_headings)
        except IndexerError:
            raise
        except Exception as error:
            raise IndexerError('NATIVE_EXTRACTION_FAILED', 'HWPX 본문 구조를 읽을 수 없습니다.', 422) from error

    def _hwpx_walk(self, root, archive, entries, location, outline_levels=None, list_properties=None, *, infer_numbered=True):
        outline_levels = outline_levels or {}
        list_properties = list_properties or set()
        list_nodes = set()
        if infer_numbered:
            for parent in root.iter():
                children = list(parent)
                texts = [(''.join(''.join(item.itertext()) for item in child.iter() if _local(item.tag) == 't')
                          if _local(child.tag) == 'p' and not any(_local(item.tag) in {'tbl', 'pic'} for item in child.iter()) else '')
                         for child in children]
                list_nodes.update(id(children[index]) for index in self._consecutive_numbered_items(texts))
        paragraph_index = 0
        def walk(node, container=None):
            nonlocal paragraph_index
            tag = _local(node.tag)
            if tag == 'p':
                paragraph_index += 1
                container = f"{location['section']}#paragraph-{paragraph_index}"
                level = outline_levels.get(node.get('paraPrIDRef')) or outline_levels.get('style:' + node.get('styleIDRef', ''))
                if not any(_local(child.tag) in {'tbl', 'pic'} for child in node.iter()):
                    text = ''.join(''.join(child.itertext()) for child in node.iter() if _local(child.tag) == 't').strip()
                    is_list = (id(node) in list_nodes or node.get('paraPrIDRef') in list_properties
                               or 'style:' + node.get('styleIDRef', '') in list_properties)
                    inferred = not level and infer_numbered and not is_list and bool(self._numbered_heading_level(text))
                    level = level or (self._numbered_heading_level(text) if infer_numbered and not is_list else None)
                    if text and level:
                        self.add('heading', text, f'<h{level}>{escape(text)}</h{level}>', location, container=container)
                        if inferred:
                            self.elements[-1]['_heading_origin'] = 'inferred'
                        return
            if tag == 'tbl':
                rows = []
                occupied = set()
                table_rows = [n for n in node if _local(n.tag) == 'tr']
                if not table_rows or len(table_rows) > 5000:
                    raise ValueError('invalid table rows')
                for row_index, row in enumerate(table_rows):
                    cells = []
                    column = 0
                    for cell in (n for n in row if _local(n.tag) == 'tc'):
                        while (row_index, column) in occupied:
                            column += 1
                        address = next((n for n in cell if _local(n.tag) == 'cellAddr'), None)
                        if address is not None:
                            row_addr, col_addr = address.get('rowAddr', ''), address.get('colAddr', '')
                            if not row_addr.isdigit() or not col_addr.isdigit() or int(row_addr) != row_index:
                                raise ValueError('invalid table address')
                            target = int(col_addr)
                            if target < column or target >= 100:
                                raise ValueError('invalid table column')
                            while column < target:
                                if (row_index, column) not in occupied:
                                    cells.append('<td></td>')
                                column += 1
                        span = next((n for n in cell if _local(n.tag) == 'cellSpan'), None)
                        rs, cs = (span.get('rowSpan', '1'), span.get('colSpan', '1')) if span is not None else ('1', '1')
                        if not rs.isdigit() or not cs.isdigit() or not 1 <= int(rs) <= 5000 or not 1 <= int(cs) <= 100:
                            raise ValueError('invalid table span')
                        if int(rs) * int(cs) > 25000:
                            raise ValueError('invalid table span')
                        positions = {(r, c) for r in range(row_index, row_index + int(rs))
                                     for c in range(column, column + int(cs))}
                        if (row_index + int(rs) > len(table_rows) or column + int(cs) > 100
                            or occupied.intersection(positions) or len(occupied) + len(positions) > 25000):
                            raise ValueError('invalid table span')
                        occupied.update(positions)
                        value = ' '.join(''.join(n.itertext()) for n in cell.iter() if _local(n.tag) == 't')
                        tag = 'th' if cell.get('header') in {'1', 'true', 'TRUE'} else 'td'
                        cells.append(f'<{tag} rowspan="{rs}" colspan="{cs}">{escape(value)}</{tag}>')
                        column += int(cs)
                    rows.append('<tr>' + ''.join(cells) + '</tr>')
                self.add('table', html='<table>' + ''.join(rows) + '</table>', location=location, container=container)
                nested = list(node.iter())[1:]
                if any(_local(n.tag) == 'tbl' for n in nested):
                    self.warning('중첩 표의 구조는 직접 추출하지 않았습니다.')
                for picture in (n for n in nested if _local(n.tag) == 'pic'):
                    walk(picture, container)
                return
            if tag == 'pic':
                refs = [v for n in node.iter() for k, v in n.attrib.items() if _local(k) == 'binaryItemIDRef']
                for ref in refs:
                    href = entries.get(ref, '')
                    names = (href, 'Contents/' + href)
                    name = next((n for n in names if n in archive.namelist()), None)
                    if not name:
                        self.warning('원본을 찾을 수 없는 이미지가 있습니다.')
                    else:
                        self.image(archive.read(name), Path(name).suffix, location, container=container)
                if not refs:
                    self.warning('참조를 확인할 수 없는 이미지가 있습니다.')
                return
            if tag in {'chart', 'rect', 'ellipse', 'line', 'polygon', 'container'}:
                self.warning('도형 또는 차트의 관계는 직접 추출하지 않았습니다.')
            if tag == 't':
                self.add('paragraph', ''.join(node.itertext()), location=location, container=container)
                return
            for child in node:
                walk(child, container)
        walk(root)


def apply_xlsx_review(raw, elements, reviews):
    """Replace structure elements while retaining the base job's visual analysis."""
    from indexer.xlsx_structure import SheetProposal, row_candidates, validate

    sheets = []
    for original in raw['xlsx_sheets']:
        sheet = dict(original)
        if sheet['name'] in reviews:
            proposal = SheetProposal.model_validate(reviews[sheet['name']])
            sheet.update(regions=validate(sheet, proposal, 'user'), excluded=proposal.excluded,
                         analysis_status='reviewed')
            sheet.pop('suggested_review', None)
        sheets.append(sheet)
    by_name = {sheet['name']: sheet for sheet in sheets}
    extractor = NativeExtractor(None, 'native_text')
    for element in elements:
        region = element.get('_xlsx_region')
        if region and region['sheet'] in reviews:
            continue
        extractor.elements.append(element)
        if element.get('_xlsx_sheet') in reviews:
            extractor._xlsx_regions(by_name[element['_xlsx_sheet']])
    relations, complete = row_candidates(sheets)
    return ({**raw, 'xlsx_sheets': sheets, 'xlsx_row_relations': relations, 'xlsx_row_relations_complete': complete},
            extractor.elements)
