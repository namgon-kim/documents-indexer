"""Spreadsheet structure proposals and validation against native cells."""

import hashlib
import json
import logging
import traceback
from collections import Counter, defaultdict
from itertools import combinations, product
from typing import Annotated, Literal

from openpyxl.utils import get_column_letter
from openpyxl.utils.cell import coordinate_to_tuple, range_boundaries
from pydantic import BaseModel, ConfigDict, Field

from indexer.office_locations import valid_cell

LOG = logging.getLogger(__name__)
STRUCTURE_VERSION = 2
MAX_HEADER_ROWS = 5
# Blank rows tolerated inside one inferred table. Larger gaps start a new block.
MAX_INNER_BLANK_ROWS = 2
Address = Annotated[str, Field(pattern=r'^[A-Z]{1,3}[1-9][0-9]{0,6}$', max_length=10)]


class RegionProposal(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    start: Address
    end: Address
    kind: Literal['table', 'title', 'key_value', 'code', 'text', 'unknown']
    # Header cells form a band of up to MAX_HEADER_ROWS rows above the data. A merged
    # header cell spans the columns of its merged range; lower header rows refine it.
    headers: list[Address] = Field(max_length=500)
    title: Address | None
    uncertain: bool


class SheetProposal(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    regions: list[RegionProposal] = Field(max_length=500)
    excluded: dict[Address, Annotated[str, Field(min_length=1, max_length=500)]] = Field(max_length=25000)


def address(row, column):
    return f'{get_column_letter(column)}{row}'


def overlaps(a, b):
    return a[0] <= b[2] and b[0] <= a[2] and a[1] <= b[3] and b[1] <= a[3]


def contains(bounds, point):
    row, col = point
    return bounds[0] <= col <= bounds[2] and bounds[1] <= row <= bounds[3]


def occupied(sheet):
    return {cell['address']: cell for cell in [*sheet['cells'], *sheet['hidden_merged_cells']]
            if cell.get('has_content')}



def verification(region):
    origin = region.get('origin')
    if origin == 'explicit' and region.get('table_name'):
        return 'explicit'
    if origin == 'user' and region.get('status') == 'verified' and not region.get('uncertain'):
        return 'user_verified'
    if origin == 'llm':
        return 'llm_validated'
    return 'inferred'


def confirmed(region):
    return verification(region) in {'explicit', 'user_verified'}


def value_kind(cell):
    """The typed value category used for structure decisions (formulas use their stored result)."""
    kind = cell.get('cached_value_type') if cell.get('formula') else cell.get('value_type')
    return 'number' if kind in {'integer', 'number', 'duration'} else kind


def is_text(cell):
    return value_kind(cell) == 'string' and bool(cell['display_text'].strip()) and not cell['hidden_merged']


def header_layout(headers, cells, merged, area):
    """Validate a header band and return (first_row, last_row, {column: [header path]}).

    A merged header covers the columns of its merged range. Each column's path lists
    the header cells above its data from top to bottom; the last item is the leaf.
    Columns may stay unlabeled, but two columns can never share the same leaf.
    """
    left, _, right, _ = area
    if len(set(headers)) != len(headers):
        raise ValueError('헤더 주소가 중복됩니다')
    spans = {}
    for key in headers:
        row, col = coordinate_to_tuple(key)
        cell = cells[key]
        if cell['hidden_merged'] or not cell['display_text'].strip():
            raise ValueError('헤더에는 값이 있는 표시 셀을 지정해 주세요')
        ref = cell.get('merged_range')
        spans[key] = merged.get(ref, (col, row, col, row))
    first = min(span[1] for span in spans.values())
    last = max(span[3] for span in spans.values())
    if last - first + 1 > MAX_HEADER_ROWS or any(span[0] < left or span[2] > right for span in spans.values()):
        raise ValueError(f'헤더는 표 안의 연속된 {MAX_HEADER_ROWS}개 행 이내에 지정해 주세요')
    coverage = {}
    for key, (a, b, c, d) in spans.items():
        for row in range(b, d + 1):
            for col in range(a, c + 1):
                if (row, col) in coverage:
                    raise ValueError('헤더 셀이 서로 겹칩니다')
                coverage[row, col] = key
    paths = {}
    for col in range(left, right + 1):
        path = []
        for row in range(first, last + 1):
            key = coverage.get((row, col))
            if key and (not path or path[-1] != key):
                path.append(key)
        paths[col] = path
    leaves = [path[-1] for path in paths.values() if path]
    if len(leaves) != len(set(leaves)):
        raise ValueError('헤더를 표의 각 열에 하나씩 지정해 주세요')
    return first, last, paths


def review_basis(sheet):
    """Fingerprint the source structure a review is validated against."""
    cells = occupied(sheet)
    payload = {'version': STRUCTURE_VERSION, 'bounds': list(sheet['bounds']),
               'merged': sorted(sheet['merged_ranges']),
               'tables': sorted([table['name'], table['ref'], list(table['headers']), table.get('totals_rows', 0)]
                                for table in sheet['tables']),
               'cells': sorted([key, bool(cell['hidden_merged']), bool(cell['display_text'].strip())]
                               for key, cell in cells.items())}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def safe_local_structure(sheet):
    try:
        return local_structure(sheet)
    except (KeyError, ValueError, IndexError, TypeError) as error:
        # Log stack frames and exception type without echoing workbook contents.
        LOG.warning('XLSX structure inference failed (%s)\n%s', type(error).__name__,
                    ''.join(traceback.format_tb(error.__traceback__)))
        return fallback_structure(sheet), []


def fallback_structure(sheet):
    if not occupied(sheet):
        return []
    return [_region(sheet['bounds']).model_dump() |
            {'origin': 'inferred', 'status': 'unverified', 'verification': 'inferred'}]


def validate(sheet, proposal, origin):
    cells = occupied(sheet)
    positions = {key: coordinate_to_tuple(key) for key in cells}
    bounds = tuple(sheet['bounds'])
    merged_by_ref = {ref: range_boundaries(ref) for ref in sheet['merged_ranges']}
    merged = list(merged_by_ref.values())
    registered = {range_boundaries(table['ref']): table for table in sheet['tables']}
    excluded = proposal.excluded
    if set(excluded) - cells.keys() or any(not reason.strip() for reason in excluded.values()):
        raise ValueError('실제 원본 셀 주소와 제외 이유를 지정해 주세요')
    seen, claimed, output = [], set(), []
    for region in proposal.regions:
        if not valid_cell(region.start) or not valid_cell(region.end):
            raise ValueError('잘못된 영역 좌표입니다')
        left, top, right, bottom = area = range_boundaries(f'{region.start}:{region.end}')
        if (left > right or top > bottom or not contains(bounds, (top, left))
                or not contains(bounds, (bottom, right)) or any(overlaps(area, other) for other in seen)):
            raise ValueError('영역 범위가 시트 밖에 있거나 겹칩니다')
        if any(overlaps(area, merge) and not (contains(area, (merge[1], merge[0]))
                   and contains(area, (merge[3], merge[2]))) for merge in merged):
            raise ValueError('영역이 병합 범위를 가릅니다')
        table = registered.get(area)
        if any(overlaps(area, ref) and area != ref for ref in registered):
            raise ValueError('Excel에 등록된 표 범위를 변경할 수 없습니다')
        if table and (region.kind != 'table' or region.headers != table['headers']):
            raise ValueError('Excel에 등록된 표의 종류와 헤더를 유지해 주세요')
        members = {key for key, position in positions.items() if key not in excluded and contains(area, position)}
        headers = region.headers
        if (not members and not table) or (headers and region.kind != 'table'):
            raise ValueError('영역의 종류와 셀 내용을 확인해 주세요')
        if any(key not in members or cells[key]['hidden_merged'] for key in headers):
            raise ValueError('헤더가 영역의 표시 셀에 속해야 합니다')
        if headers:
            header_top, header_bottom, _ = header_layout(headers, cells, merged_by_ref, area)
            if any(positions[key][0] < header_top and key != region.title for key in members):
                raise ValueError('헤더 앞에 제목 외의 값이 있습니다')
            if any(header_top <= positions[key][0] <= header_bottom and key not in headers
                   and not cells[key]['hidden_merged'] for key in members):
                raise ValueError('헤더 행에 헤더가 아닌 값이 있습니다')
        if region.title and (region.title not in members or cells[region.title]['hidden_merged']
                             or region.title in headers):
            raise ValueError('제목이 영역의 표시 셀에 속해야 합니다')
        if region.kind == 'title' and (not region.title or len(members) != 1):
            raise ValueError('제목 영역에는 제목 셀 하나를 지정해 주세요')
        if (region.title and region.kind in {'table', 'key_value'}
                and any(positions[key][0] <= positions[region.title][0] for key in members if key != region.title)):
            raise ValueError('제목은 표나 항목·값의 데이터 앞에 있어야 합니다')
        if region.kind == 'key_value':
            row_counts = defaultdict(int)
            for key in members - {region.title}:
                row_counts[positions[key][0]] += 1
            if not row_counts or any(count < 2 or count % 2 for count in row_counts.values()):
                raise ValueError('항목·값 영역의 각 행에는 짝수 개의 셀이 필요합니다')
        seen.append(area)
        claimed.update(members)
        validated = {**region.model_dump(), 'origin': 'explicit' if table else origin,
                     'status': 'verified' if table or (origin == 'user' and not region.uncertain
                                                       and region.kind != 'unknown') else 'unverified',
                     'table_name': table['name'] if table else None,
                     'totals_rows': table['totals_rows'] if table else 0,
                     'totals_rows_basis': (table.get('totals_rows_basis', 'legacy' if table['totals_rows'] else 'none')
                                           if table else 'none')}
        output.append({**validated, 'verification': verification(validated)})
    if registered.keys() - set(seen) or set(excluded) != cells.keys() - claimed:
        raise ValueError('각 원본 셀을 한 영역에 포함하거나 제외 이유를 지정해 주세요')
    return sorted(output, key=lambda item: coordinate_to_tuple(item['start']))


def _region(box, kind='unknown', headers=(), title=None, uncertain=True):
    left, top, right, bottom = box
    return RegionProposal(start=address(top, left), end=address(bottom, right), kind=kind,
                          headers=list(headers), title=title, uncertain=uncertain)


def _bounds(boxes):
    return (min(box[0] for box in boxes), min(box[1] for box in boxes),
            max(box[2] for box in boxes), max(box[3] for box in boxes))


def _visible(entries):
    return {col: cell for col, cell in entries.items() if not cell['hidden_merged']}


def _header_row(entries, body):
    """Whether one row reads as column labels for the (column, cell) pairs below it."""
    visible = _visible(entries)
    if len(visible) < 2 or not all(is_text(cell) for cell in visible.values()):
        return False
    if all(cell['bold'] for cell in visible.values()):
        return True
    kinds = defaultdict(set)
    for col, cell in body:
        if col in visible and not cell['hidden_merged'] and value_kind(cell) not in {None, 'null'}:
            kinds[col].add(value_kind(cell))
    non_text = sum(1 for col in visible if kinds[col] and 'string' not in kinds[col])
    return non_text > 0 and non_text * 2 >= len(visible)


def _inferred_header(cells, by_row, merges, whole):
    """Infer a header band and an optional title cell from source cells only.

    Header rows contain only text. Vertical merges extend the band and a horizontal
    merge lets the next row refine its columns. The band must be followed by data
    and is accepted only when it is bold or the data below it is not text, so a
    text-only block keeps its unknown structure.
    """
    bottom = whole[3]
    merged = {ref: range_boundaries(ref) for ref in merges}
    rows = sorted(by_row)
    start, title_key = rows[0], None
    first = _visible(by_row[start])
    if len(first) == 1 and len(rows) > 2:
        (col, cell), = first.items()
        if is_text(cell) and '\n' not in cell['display_text'] and rows[1] - start <= 2:
            title_key, start = address(start, col), rows[1]
    band_end, reach, row = None, start, start
    while row <= bottom and row - start < MAX_HEADER_ROWS:
        entries = _visible(by_row.get(row, {}))
        if not entries or not all(is_text(cell) for cell in entries.values()):
            break
        band_end = row
        spans = [merged[cell['merged_range']] for cell in entries.values() if cell.get('merged_range') in merged]
        reach = max([reach, row, *(span[3] for span in spans)])
        if reach > row or any(span[2] > span[0] for span in spans):
            row += 1
            continue
        break
    if band_end is None or reach > band_end:
        return None
    headers = [address(r, col) for r in range(start, band_end + 1) for col in sorted(_visible(by_row.get(r, {})))]
    body = [(col, cell) for r in rows if r > band_end for col, cell in by_row[r].items()]
    if not body or len(headers) < 2:
        return None
    if band_end == start:
        if not _header_row(by_row[start], body):
            return None
    elif not all(cells[key]['bold'] for key in headers) and not any(
            not cell['hidden_merged'] and value_kind(cell) not in {None, 'null', 'string'} for _, cell in body):
        # Multi-row bands still need text labels above non-text data.
        return None
    try:
        header_layout(headers, cells, merged, whole)
    except ValueError:
        return None
    return headers, title_key


def _blocks(cells, merges, limit=200):
    """Split cells into blocks of touching cells (diagonals included).

    Merged ranges join the cells they cover. Overlapping block rectangles are
    combined, and a block continues across a few blank rows when the rows below
    keep the same columns and value types without starting a new header.
    """
    positions = {key: coordinate_to_tuple(key) for key in cells}
    merge_boxes = {ref: range_boundaries(ref) for ref in merges}
    parent = {}

    def find(node):
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(a, b):
        a, b = find(a), find(b)
        if a != b:
            parent[b] = a

    grid = {}
    for key, point in positions.items():
        parent[key] = key
        grid[point] = key
    for ref, (left, top, right, bottom) in merge_boxes.items():
        node = 'merge:' + ref
        parent[node] = node
        for row in range(top, bottom + 1):
            for col in range(left, right + 1):
                if (row, col) in grid:
                    union(grid[row, col], node)
                else:
                    grid[row, col] = node
    for (row, col), node in grid.items():
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                other = grid.get((row + dr, col + dc))
                if other is not None:
                    union(node, other)
    groups = defaultdict(lambda: {'cells': set(), 'merges': set()})
    for node in parent:
        group = groups[find(node)]
        if node.startswith('merge:'):
            group['merges'].add(node.removeprefix('merge:'))
        else:
            group['cells'].add(node)

    def box(group):
        return _bounds([*(merge_boxes[ref] for ref in group['merges']),
                        *((col, row, col, row) for row, col in (positions[key] for key in group['cells']))])

    blocks = [{**group, 'box': box(group)} for group in groups.values()]
    if len(blocks) > limit:
        # Scattered cells: keep the bounded single-rectangle behaviour.
        whole = {'cells': set(cells), 'merges': set(merges)}
        return [{**whole, 'box': box(whole)}], []

    def combine(target, source):
        target['cells'] |= source['cells']
        target['merges'] |= source['merges']
        target['box'] = _bounds([target['box'], source['box']])
        blocks.remove(source)

    changed = True
    while changed:
        changed = False
        for first, second in combinations(blocks, 2):
            if overlaps(first['box'], second['box']):
                combine(first, second)
                changed = True
                break
    blocks = [block for block in blocks if block['cells']]
    hints = []
    changed = True
    while changed:
        changed = False
        for lower in sorted(blocks, key=lambda block: block['box'][1]):
            uppers = sorted((block for block in blocks if block is not lower and block['box'][3] < lower['box'][1]
                             and max(block['box'][0], lower['box'][0]) <= min(block['box'][2], lower['box'][2])),
                            key=lambda block: -block['box'][3])
            for upper in uppers:
                joined = _bounds([upper['box'], lower['box']])
                if any(overlaps(joined, other['box']) for other in blocks if other is not upper and other is not lower):
                    continue
                if _continues(upper, lower, cells, positions):
                    combine(upper, lower)
                    changed = True
                break
            if changed:
                break
    for upper, lower in combinations(sorted(blocks, key=lambda block: block['box'][1]), 2):
        if (0 < lower['box'][1] - upper['box'][3] - 1 <= MAX_INNER_BLANK_ROWS
                and max(upper['box'][0], lower['box'][0]) <= min(upper['box'][2], lower['box'][2])):
            hints.append({'axis': 'row', 'after': upper['box'][3], 'reason': 'separate_block'})
    return blocks, hints


def _block_rows(block, cells, positions):
    by_row = defaultdict(dict)
    for key in block['cells']:
        row, col = positions[key]
        by_row[row][col] = cells[key]
    return by_row


def _column_kinds(by_row, rows):
    kinds = defaultdict(Counter)
    for row in rows:
        for col, cell in by_row[row].items():
            kind = value_kind(cell)
            if not cell['hidden_merged'] and kind not in {None, 'null'}:
                kinds[col][kind] += 1
    return kinds


def _continues(upper, lower, cells, positions):
    """Whether `lower` continues the block above it across blank rows."""
    ul, _, ur, ub = upper['box']
    ll, lt, lr, _ = lower['box']
    gap = lt - ub - 1
    if not 1 <= gap <= MAX_INNER_BLANK_ROWS or len(lower['cells']) < 2:
        return False
    up, low = _block_rows(upper, cells, positions), _block_rows(lower, cells, positions)
    up_rows, low_rows = sorted(up), sorted(low)
    lower_first = low[low_rows[0]]
    lower_body = [(col, cell) for row in low_rows[1:] for col, cell in low[row].items()]
    if len(upper['cells']) == 1:
        # A title separated from its table by one blank row.
        (cell,) = (cells[key] for key in upper['cells'])
        return gap == 1 and ll <= ul <= lr and is_text(cell) and _header_row(lower_first, lower_body)
    if ll < ul or lr > ur or _header_row(lower_first, lower_body):
        return False
    upper_body = [(col, cell) for row in up_rows[1:] for col, cell in up[row].items()]
    body_rows = up_rows[1:] if _header_row(up[up_rows[0]], upper_body) else up_rows
    above, below = _column_kinds(up, body_rows), _column_kinds(low, low_rows)
    shared = [col for col in below if above.get(col)]
    if not shared:
        return False
    agree = sum(above[col].most_common(1)[0][0] == below[col].most_common(1)[0][0] for col in shared)
    return agree * 2 > len(shared)


def _local_regions(cells, merges):
    positions = {key: coordinate_to_tuple(key) for key in cells}
    boxes = [range_boundaries(ref) for ref in merges]
    boxes.extend((col, row, col, row) for row, col in positions.values())
    if not boxes:
        return [], []
    left, top, right, bottom = whole = _bounds(boxes)
    by_row = defaultdict(dict)
    for key, (row, col) in positions.items():
        by_row[row][col] = cells[key]

    blank_rows = [row for row in range(top, bottom + 1)
                  if row not in by_row and not any(a[1] <= row <= a[3] for a in boxes[:len(merges)])]
    candidates = [{'axis': 'row', 'after': row - 1, 'reason': 'blank_row'} for row in blank_rows]
    # A repeated styled header can start a new region even on consecutive rows.
    starts = [row for row, entries in sorted(by_row.items()) if len(entries) == right - left + 1
              and len(entries) >= 2 and all(item['bold'] and item['type'] in {'s', 'inlineStr'}
                                           and not item.get('merged_range') for item in entries.values())]
    regions = []
    title_key = None
    head = by_row.get(top, {})
    if starts and top < starts[0] <= top + 2 and len(head) == 1:
        column, cell = next(iter(head.items()))
        if cell['bold'] and cell['type'] in {'s', 'inlineStr'} and all(row == top or row >= starts[0] for row in by_row):
            title_key = address(top, column)
    if starts and (starts[0] == top or title_key):
        bands = list(zip(starts, [row - 1 for row in starts[1:]] + [bottom], strict=True))
        if all(end > start and any(by_row.get(row) for row in range(start + 1, end + 1))
               for start, end in bands):
            regions = [_region((left, top if index == 0 and title_key else start, right, end), 'table',
                        [address(start, col) for col in range(left, right + 1)], title=title_key if index == 0 else None)
                       for index, (start, end) in enumerate(bands)]
    # Adjacent tables can have distinct column-header style runs without a blank column.
    run_starts = [left]
    if len(head) == right - left + 1 and all(item['bold'] and item['type'] in {'s', 'inlineStr'}
                                              and not item.get('merged_range') for item in head.values()):
        run_starts += [col for col in range(left + 1, right + 1)
                       if head[col]['style_id'] != head[col - 1]['style_id']]
    runs = list(zip(run_starts, [col - 1 for col in run_starts[1:]] + [right], strict=True))
    if len(runs) > 1 and all(end > start and any(start <= col <= end for row, col in positions.values()
                                                if row > top) for start, end in runs):
        candidates.extend({'axis': 'column', 'after': start - 1, 'reason': 'header_style_change'}
                          for start in run_starts[1:])
        if len(regions) <= 1:
            regions = [_region((start, top, end, bottom), 'table',
                       [address(top, col) for col in range(start, end + 1)]) for start, end in runs]
    if regions:
        return regions, candidates
    if len(cells) > 1 and (inferred := _inferred_header(cells, by_row, merges, whole)):
        headers, title_key = inferred
        return [_region(whole, 'table', headers, title=title_key)], candidates
    if len(cells) == 1:
        key, cell = next(iter(cells.items()))
        if cell['bold'] and not cell['hidden_merged'] and '\n' not in cell['display_text']:
            return [_region(whole, 'title', title=key)], candidates
        if cell['type'] in {'s', 'inlineStr'} and any(line.startswith(('  ', '\t'))
                                                      for line in cell['display_text'].splitlines()[1:]):
            return [_region(whole, 'code')], candidates
        return [_region(whole, 'text')], candidates
    if top == bottom and len(cells) == 2 and head[min(head)]['bold'] and not head[max(head)]['bold']:
        return [_region(whole, 'key_value')], candidates
    return [_region(whole)], candidates


def local_structure(sheet):
    cells = occupied(sheet)
    declared = [_region(range_boundaries(table['ref']), 'table', table['headers'], uncertain=False)
                for table in sheet['tables']]
    # Subtract declared rectangles before bounding any remaining cells. This keeps
    # adjacent registered tables distinct even when every row and column is occupied.
    free = [tuple(sheet['bounds'])]
    for table in sheet['tables']:
        ref = range_boundaries(table['ref'])
        remaining = []
        for box in free:
            if not overlaps(ref, box):
                remaining.append(box)
                continue
            a, b, c, d = box
            left, top, right, bottom = ref
            remaining.extend(part for part in (
                (a, b, c, min(d, top - 1)), (a, max(b, bottom + 1), c, d),
                (a, max(b, top), min(c, left - 1), min(d, bottom)),
                (max(a, right + 1), max(b, top), c, min(d, bottom)),
            ) if part[0] <= part[2] and part[1] <= part[3])
        free = remaining
    fragments, blocks, candidates = [], [], []
    for box in free:
        selected = {key: item for key, item in cells.items() if contains(box, coordinate_to_tuple(key))}
        merges = [ref for ref in sheet['merged_ranges'] if overlaps(box, range_boundaries(ref))]
        if not selected:
            continue
        # Separate blocks first, then interpret each block's own header and title.
        found, hints = _blocks(selected, merges)
        candidates.extend(hints)
        for block in found:
            regions, more = _local_regions({key: selected[key] for key in block['cells']}, sorted(block['merges']))
            fragments.extend(regions)
            candidates.extend(more)
            blocks.append(_region(block['box']))
    # A styled boundary can cross a merge. Fall back to unknown blocks, then to the
    # residual rectangles, and keep the sheet searchable if declarations conflict.
    fallback = [_region(box) for box in free if any(contains(box, coordinate_to_tuple(key)) for key in cells)]
    for regions in (fragments, blocks, fallback):
        try:
            return validate(sheet, SheetProposal(regions=declared + regions, excluded={}), 'inferred'), candidates
        except ValueError:
            continue
    return fallback_structure(sheet), candidates


def identity_rows(sheet, cells=None):
    """Read the legacy ID/name identity columns from confirmed tables."""
    aliases = {'id': 'id', 'code': 'id', 'identifier': 'id', '번호': 'id', '코드': 'id', '학번': 'id',
               'name': 'name', '이름': 'name', '명칭': 'name', '성명': 'name'}
    cells = occupied(sheet) if cells is None else cells
    for region in sheet['regions']:
        if not confirmed(region) or not region['headers']:
            continue
        labels = defaultdict(list)
        for key in region['headers']:
            row, col = coordinate_to_tuple(key)
            label = aliases.get(cells[key]['display_text'].strip().casefold())
            if label:
                labels[label].append(col)
        if set(labels) != {'id', 'name'} or any(len(cols) != 1 for cols in labels.values()):
            continue
        end, _ = coordinate_to_tuple(region['end'])
        for data_row in range(row + 1, end - region['totals_rows'] + 1):
            keys = [address(data_row, labels[label][0]) for label in ('id', 'name')]
            if any(key not in cells or key in sheet['excluded'] or cells[key]['hidden_merged'] or cells[key].get('merged_range')
                   or cells[key].get('cache_status') == 'missing' for key in keys):
                continue
            values = tuple(cells[key]['display_text'].strip().casefold() for key in keys)
            if all(values):
                yield values[0], {'sheet': sheet['name'], 'row': data_row, 'cells': keys,
                                  'region_start': region['start'], 'name_key': values[1]}


def row_candidates(sheets):
    """Find exact matches and identifier conflicts under confirmed headers."""
    index = defaultdict(lambda: defaultdict(list))
    for sheet in sheets:
        for identifier, point in identity_rows(sheet):
            index[identifier][sheet['name']].append(point)
    relations = []
    # Bound ambiguous fan-out. Preserve both original row addresses on each candidate.
    for by_sheet in index.values():
        for left, right in combinations(by_sheet.values(), 2):
            ambiguous = len(left) != 1 or len(right) != 1
            for a, b in product(left, right):
                if len(relations) == 5000:
                    return relations, False
                same_name = a['name_key'] == b['name_key']
                relations.append({'left': {key: value for key, value in a.items() if key != 'name_key'},
                                  'right': {key: value for key, value in b.items() if key != 'name_key'},
                                  'status': 'unverified', 'conflict': not same_name,
                                  'ambiguous': ambiguous or not same_name, 'matching_rows': [len(left), len(right)],
                                  'evidence': ['id', 'name'] if same_name else ['id']})
    return relations, True
