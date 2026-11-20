"""Create approximate sheet previews for embedded spreadsheet charts."""

import json
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from pypdf import PdfReader

LOG = logging.getLogger(__name__)


def renderer_status():
    office = shutil.which('soffice')
    if not office:
        mac_office = Path('/Applications/LibreOffice.app/Contents/MacOS/soffice')
        office = str(mac_office) if mac_office.is_file() else None
    rasterizer = shutil.which('pdftoppm')
    return office, rasterizer


def chart_preview(path: Path, sheet_name: str, chart_index: int) -> bytes | None:
    """Render a chart's approximate sheet region; never alter the input workbook."""
    office, rasterizer = renderer_status()
    if not office or not rasterizer:
        LOG.warning(json.dumps({'event': 'xlsx_chart_preview_failed', 'reason': 'renderer_missing',
                                'soffice_available': bool(office), 'pdftoppm_available': bool(rasterizer)}))
        return None
    try:
        with tempfile.TemporaryDirectory(prefix='xlsx-chart-') as directory:
            root = Path(directory)
            workbook = load_workbook(path, data_only=False, keep_links=False)
            sheet = workbook[sheet_name]
            chart = sheet._charts[chart_index]
            anchor = getattr(chart.anchor, '_from', None)
            if anchor is None:
                LOG.warning(json.dumps({'event': 'xlsx_chart_preview_failed', 'reason': 'anchor_missing'}))
                workbook.close()
                return None
            end = getattr(chart.anchor, 'to', None)
            # One-cell anchors only specify dimensions in EMUs. Reserve a generous
            # region and fit it to one page; this is not pixel-accurate cropping.
            last_col = max(anchor.col + 1, end.col + 1) if end else anchor.col + 16
            last_row = max(anchor.row + 1, end.row + 1) if end else anchor.row + 35
            sheet.print_area = f'{get_column_letter(anchor.col + 1)}{anchor.row + 1}:{get_column_letter(last_col)}{last_row}'
            sheet.sheet_properties.pageSetUpPr.fitToPage = True
            sheet.page_setup.fitToWidth = 1
            sheet.page_setup.fitToHeight = 1
            sheet.page_setup.orientation = 'landscape'
            for other in workbook.worksheets:
                other.sheet_state = 'visible' if other.title == sheet_name else 'hidden'
            workbook.active = workbook.sheetnames.index(sheet_name)
            source = root / 'preview.xlsx'
            workbook.save(source)
            workbook.close()
            proc = subprocess.run([office, '-env:UserInstallation=' + (root / 'profile').as_uri(),
                                   '--headless', '--convert-to', 'pdf', '--outdir', str(root), str(source)],
                                  capture_output=True, timeout=90, check=False)
            pdf = root / 'preview.pdf'
            if proc.returncode or not pdf.is_file() or len(PdfReader(pdf).pages) != 1:
                LOG.warning(json.dumps({'event': 'xlsx_chart_preview_failed', 'reason': 'pdf_render_failed',
                                        'exit_code': proc.returncode}))
                return None
            proc = subprocess.run([rasterizer, '-f', '1', '-l', '1', '-singlefile', '-r', '110', '-png',
                                   str(pdf), str(root / 'chart')], capture_output=True, timeout=30, check=False)
            image = root / 'chart.png'
            if proc.returncode or not image.is_file():
                LOG.warning(json.dumps({'event': 'xlsx_chart_preview_failed', 'reason': 'rasterize_failed',
                                        'exit_code': proc.returncode}))
                return None
            return image.read_bytes()
    except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError) as error:
        LOG.warning(json.dumps({'event': 'xlsx_chart_preview_failed', 'reason': 'exception',
                                'error_type': type(error).__name__}))
        return None
