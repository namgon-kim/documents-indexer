"""Evaluate bounded, typed query plans over complete verified XLSX table ranges."""

import math
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal, localcontext
from typing import Literal
from uuid import UUID

from openpyxl.utils import column_index_from_string, get_column_letter
from openpyxl.utils.cell import coordinate_to_tuple, range_boundaries
from pydantic import BaseModel, ConfigDict, Field, model_validator

from indexer.contracts import IndexerError
from indexer.parsing.xlsx_format import value_category
from indexer.xlsx_structure import (
    STRUCTURE_VERSION,
    address,
    confirmed,
    contains,
    header_layout,
    occupied,
    row_candidates,
    verification,
)

Scalar = str | bool | int | float
NUMERIC = {'sum', 'average', 'min', 'max'}
# Aggregates whose value changes when the same source row is read more than once.
DUPLICATE_SENSITIVE = {'sum', 'average', 'distribution'}
COLUMN = r'^[A-Z]{1,3}$'
TABLE_START = r'^[A-Z]{1,3}[1-9][0-9]{0,6}$'
Side = Literal['left', 'right']
# Excel stores and compares numbers with 15 significant decimal digits.
EXCEL_DIGITS = 15


class TableFilter(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)
    column: str = Field(pattern=COLUMN, max_length=3)
    side: Side = 'left'
    operator: Literal['eq', 'ne', 'lt', 'lte', 'gt', 'gte', 'contains', 'is_blank', 'not_blank']
    value: Scalar | None = None
    # How a blank cell answers a value comparison. Without a choice, a blank cell
    # fails eq, range and contains tests, while `ne` leaves it undecided.
    blanks: Literal['include', 'exclude'] | None = None

    @model_validator(mode='after')
    def check_value(self):
        if (self.operator in {'is_blank', 'not_blank'}) != (self.value is None):
            raise ValueError('필터 연산자에 맞는 값을 지정해 주세요')
        if self.blanks is not None and self.operator in {'is_blank', 'not_blank'}:
            raise ValueError('빈 셀 조건에는 빈 셀 처리 방식을 따로 지정하지 않습니다')
        if isinstance(self.value, str) and len(self.value) > 1000:
            raise ValueError('필터 값이 너무 깁니다')
        return self


class QueryPlan(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    sheet: str = Field(min_length=1, max_length=31)
    start: str = Field(pattern=TABLE_START, max_length=10)
    operation: Literal['count', 'distribution', 'date_range', 'rows', 'sum', 'average', 'min', 'max']
    column: str | None = Field(default=None, pattern=COLUMN, max_length=3)
    column_side: Side = 'left'
    group_by: str | None = Field(default=None, pattern=COLUMN, max_length=3)
    group_side: Side = 'left'
    filters: list[TableFilter] = Field(default_factory=list, max_length=20)
    include_hidden_rows: bool = True
    offset: int = Field(default=0, ge=0, le=5000)
    limit: int = Field(default=100, ge=1, le=100)
    # With a join: 'pairs' aggregates each joined row pair, 'left_rows'/'right_rows'
    # count each source row once, and 'auto' uses pairs but reports duplicated values.
    aggregate_unit: Literal['auto', 'pairs', 'left_rows', 'right_rows'] = 'auto'

    @model_validator(mode='after')
    def check_column(self):
        if (self.operation in NUMERIC | {'distribution', 'date_range'}) != (self.column is not None):
            raise ValueError('집계 연산에 맞는 열을 지정해 주세요')
        if self.group_by and self.operation not in NUMERIC | {'count'}:
            raise ValueError('그룹 열은 개수와 숫자 집계에 지정할 수 있습니다')
        return self


class RowPair(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    left_row: int = Field(ge=1, le=5000)
    right_row: int = Field(ge=1, le=5000)


class ApprovedJoin(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    sheet: str = Field(min_length=1, max_length=31)
    start: str = Field(pattern=TABLE_START, max_length=10)
    approved_pairs: list[RowPair] = Field(min_length=1, max_length=5000)



class JoinKey(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    left: str = Field(pattern=COLUMN, max_length=3)
    right: str = Field(pattern=COLUMN, max_length=3)
    type: Literal['text', 'number', 'date', 'datetime', 'boolean']


class MappedJoin(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    sheet: str = Field(min_length=1, max_length=31)
    start: str = Field(pattern=TABLE_START, max_length=10)
    cardinality: Literal['one_to_one', 'one_to_many', 'many_to_one']
    keys: list[JoinKey] = Field(min_length=1, max_length=4)

    @model_validator(mode='after')
    def distinct_columns(self):
        if len({key.left for key in self.keys}) != len(self.keys) or len({key.right for key in self.keys}) != len(self.keys):
            raise ValueError('같은 쪽의 열을 연결 키에 중복 지정할 수 없습니다')
        return self


class JoinPreview(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    document_id: UUID = Field(strict=False)
    index_version: UUID = Field(strict=False)
    profile_id: str | None = Field(default=None, min_length=1, max_length=64)
    sheet: str = Field(min_length=1, max_length=31)
    start: str = Field(pattern=TABLE_START, max_length=10)
    join: MappedJoin
    include_hidden_rows: bool = True
    offset: int = Field(default=0, ge=0, le=5000)


class PublishedTableQuery(QueryPlan):
    document_id: UUID = Field(strict=False)
    index_version: UUID | None = Field(default=None, strict=False)
    profile_id: str | None = Field(default=None, min_length=1, max_length=64)
    join: ApprovedJoin | MappedJoin | None = None

    @model_validator(mode='after')
    def pin_approval(self):
        if self.join and not self.index_version:
            raise ValueError('행을 연결할 색인 버전을 지정해 주세요')
        return self


TEMPORAL = {'date': date, 'datetime': datetime, 'time': time}


def _value(cell):
    if not cell:
        return None, 'null'
    if cell['formula']:
        return cell['cached_value'], cell['cached_value_type']
    return cell['value'], cell['value_type']


def _kind(value_type):
    return 'number' if value_type in {'integer', 'number', 'duration'} else value_type


def number_key(value):
    """Compare integers exactly and binary floats at Excel's decimal precision."""
    number = Decimal(str(value))
    if isinstance(value, int):
        return number
    return Decimal(format(number, f'.{EXCEL_DIGITS}g'))


def text_key(value):
    return unicodedata.normalize('NFKC', value).strip().casefold()


def _compare(relation, actual, expected):
    if relation == 'eq':
        return actual == expected
    if relation == 'ne':
        return actual != expected
    if relation == 'lt':
        return actual < expected
    if relation == 'lte':
        return actual <= expected
    if relation == 'gt':
        return actual > expected
    return actual >= expected


def _prepare_filter(rule, types):
    """Return predicate(value, kind) -> (True | False | None, issue)."""
    value = rule.value
    relation = rule.operator
    if relation in {'is_blank', 'not_blank'}:
        return lambda cell_value, _: ((cell_value is None) == (relation == 'is_blank'), None)
    if relation == 'contains' and (not isinstance(value, str) or 'string' not in types):
        raise ValueError('포함 필터에는 문자열 열과 값이 필요합니다')
    if isinstance(value, bool) and relation not in {'eq', 'ne'}:
        raise ValueError('참·거짓 값에는 같음과 다름을 사용할 수 있습니다')
    convert = {}
    for kind in types:
        if relation == 'contains' and kind != 'string':
            continue
        if kind == 'boolean' and isinstance(value, bool) or kind == 'string' and isinstance(value, str):
            convert[kind] = value
        elif kind == 'number' and type(value) in {float, int} and (type(value) is int or math.isfinite(value)):
            convert[kind] = number_key(value)
        elif kind in TEMPORAL and isinstance(value, str):
            try:
                parsed = TEMPORAL[kind].fromisoformat(value)
                if getattr(parsed, 'tzinfo', None) is None:
                    convert[kind] = parsed
            except ValueError:
                continue
    if not convert and types:
        raise ValueError('필터 값의 타입이 표의 열과 일치하지 않습니다')

    def matches(cell_value, kind):
        if kind == 'null':
            if rule.blanks is not None:
                return rule.blanks == 'include', None
            # A blank cell is certainly not equal to, inside or within a range of a
            # value. Whether it should count as "different" depends on the question.
            return (None, 'blank_filter_value') if relation == 'ne' else (False, None)
        if kind not in convert:
            return None, 'filter_type_mismatch'
        expected = convert[kind]
        if kind in TEMPORAL:
            actual = TEMPORAL[kind].fromisoformat(cell_value)
        elif kind == 'number':
            actual = number_key(cell_value)
        else:
            actual = cell_value
        if relation == 'contains':
            return text_key(expected) in text_key(actual), None
        decision = _compare(relation, actual, expected)
        if kind == 'string' and relation in {'eq', 'ne'} and actual != expected and text_key(actual) == text_key(expected):
            # Values that differ only in case, width or surrounding spaces are
            # neither silently matched nor silently excluded.
            return None, 'text_variant'
        return decision, None

    return matches


def header_bottom(region, merged_ranges):
    """The last row of the header band, including merged header cells."""
    headers = set(region['headers'])
    if not headers:
        return None
    rows = [coordinate_to_tuple(key)[0] for key in headers]
    for ref in merged_ranges:
        left, top, _, bottom = range_boundaries(ref)
        if address(top, left) in headers:
            rows.append(bottom)
    return max(rows)


def verified_tables(raw):
    if not isinstance(raw, dict):
        return raw.catalog()
    if raw.get('xlsx_structure_version') != STRUCTURE_VERSION:
        return []
    catalog = []
    for sheet in raw['xlsx_sheets']:
        if not sheet['regions']:
            continue
        cells = occupied(sheet)
        merged = {ref: range_boundaries(ref) for ref in sheet['merged_ranges']}
        for region in sheet['regions']:
            if region['kind'] == 'table' and confirmed(region):
                area = range_boundaries(f"{region['start']}:{region['end']}")
                left, _, right, bottom = area
                columns = {get_column_letter(col): None for col in range(left, right + 1)}
                header_end = header_bottom(region, sheet['merged_ranges']) or 0
                try:
                    paths = header_layout(region['headers'], cells, merged, area)[2] if region['headers'] else {}
                except ValueError:
                    paths = {coordinate_to_tuple(key)[1]: [key] for key in region['headers']}
                for col, path in paths.items():
                    if path:
                        columns[get_column_letter(col)] = ' > '.join(cells[key]['display_text'] for key in path)
                formats = defaultdict(Counter)
                last = bottom - region.get('totals_rows', 0)
                for key, cell in cells.items():
                    row, col = coordinate_to_tuple(key)
                    if contains(area, (row, col)) and header_end < row <= last and key != region['title']:
                        kind = cell['cached_value_type'] if cell['formula'] else cell['value_type']
                        category = cell.get('value_category') or value_category(cell['number_format'], kind)
                        if category not in {'null', 'error'}:
                            formats[get_column_letter(col)][category] += 1
                catalog.append({'sheet': sheet['name'], 'start': region['start'], 'end': region['end'],
                                'title': cells[region['title']]['display_text'] if region['title'] else region['table_name'],
                                'columns': columns, 'verification': verification(region),
                                # Stored value kinds guide typed filters, e.g. 25% is stored as 0.25.
                                'column_formats': {col: counts.most_common(1)[0][0] for col, counts in formats.items()}})
    return catalog



@dataclass
class TableData:
    scope: dict
    left: int
    right: int
    values: dict
    problems: dict
    populated: set
    hidden: set
    available_rows: set
    # Cells covered by a vertical merge in the data area read the merge's value.
    derived: dict = field(default_factory=dict)
    # Table-level conditions that make results incomplete, e.g. unclear totals rows.
    notes: Counter = field(default_factory=Counter)
    # Rows that may be a totals row. They stay in range and are undecided when used.
    uncertain_rows: set = field(default_factory=set)

    def column(self, key):
        number = column_index_from_string(key)
        if not self.left <= number <= self.right:
            raise ValueError('표 범위에 속한 열을 지정해 주세요')
        return number

    def get(self, row, column):
        value, kind, issue = self.raw_cell(row, column)
        return value, _kind(kind), issue

    def raw_cell(self, row, column):
        key = row, column
        return *self.values.get(key, (None, 'null')), self.problems.get(key)

    def record(self, row):
        record = {}
        for col in range(self.left, self.right + 1):
            value, kind, issue = self.get(row, col)
            record[get_column_letter(col)] = {'value': value if issue is None else None, 'type': kind,
                                              'source': address(row, col), 'issue': issue,
                                              **({'merged_from': self.derived[row, col]} if (row, col) in self.derived else {})}
        return record



def typed_table_cell(cell, excluded):
    if excluded:
        return None, 'null', 'excluded_cell'
    if cell['hidden_merged'] or cell.get('cache_status') == 'missing':
        return None, 'null', 'hidden_merged' if cell['hidden_merged'] else 'missing_cache'
    value, value_type = _value(cell)
    return value, value_type, 'excel_error' if value_type == 'error' else None


def _table_cells(raw, sheet, area, columns):
    if not isinstance(raw, dict):
        yield from raw.table_cells(sheet['name'], area, columns)
        return
    for key, cell in occupied(sheet).items():
        position = coordinate_to_tuple(key)
        if contains(area, position):
            yield *position, *typed_table_cell(cell, key in sheet['excluded'])


def _read_table(raw, sheet_name, region_start, include_hidden, selected=None):
    sheet = (next((item for item in raw['xlsx_sheets'] if item['name'] == sheet_name), None)
             if isinstance(raw, dict) else raw.sheet(sheet_name))
    region = next((r for r in sheet['regions'] if r['start'] == region_start and r['kind'] == 'table'
                   and confirmed(r)), None) if sheet else None
    if not region:
        raise ValueError('Excel에 등록된 표 또는 사용자가 확인한 표를 지정해 주세요')
    area = range_boundaries(f"{region['start']}:{region['end']}")
    left, top, right, bottom = area
    band = header_bottom(region, sheet['merged_ranges'])
    start = band + 1 if band is not None else top
    if region['title'] and coordinate_to_tuple(region['title'])[0] == start:
        start += 1
    notes = Counter()
    totals = region.get('totals_rows', 0)
    basis = region.get('totals_rows_basis', 'legacy' if totals else 'none')
    uncertain = set()
    if basis == 'inconsistent':
        notes['totals_rows_inconsistent'] += 1
    if totals > max(0, bottom - start + 1):
        notes['totals_rows_inconsistent'] += 1
        totals = 0
    if totals and basis != 'declared':
        # Older artifacts derived a totals row from `totalsRowShown`, so the last
        # row may be data. Keep it in range; a query that needs it stays undecided.
        uncertain = set(range(bottom - totals + 1, bottom + 1))
        totals = 0
    end = bottom - totals
    data_area = left, start, right, end
    columns = None if selected is None or isinstance(raw, dict) else {column_index_from_string(key) for key in selected}
    if columns is not None and any(not left <= column <= right for column in columns):
        raise ValueError('표 범위에 속한 열을 지정해 주세요')
    values, problems = {}, {}
    populated, available_rows = set(), set()
    if columns is not None:
        for row, available in raw.row_availability(sheet_name, data_area):
            populated.add(row)
            if available:
                available_rows.add(row)
    title = coordinate_to_tuple(region['title']) if region['title'] else None
    for row, col, value, kind, issue in _table_cells(raw, sheet, data_area, columns):
        key = row, col
        if key == title:
            continue
        populated.add(row)
        if issue:
            problems[key] = issue
        else:
            available_rows.add(row)
        if issue in {None, 'excel_error'}:
            values[key] = value, kind
    derived = {}
    for ref in sheet['merged_ranges']:
        a, b, c, d = range_boundaries(ref)
        if not contains(area, (b, a)):
            continue
        # A single-column merge inside the data rows is a group label for the rows
        # it spans. Other merges leave their covered cells without a value.
        group = a == c and d > b and start <= b <= end and (b, a) in values and (b, a) not in problems
        for row in range(max(start, b), min(d, end) + 1):
            for col in range(a, c + 1):
                if (row, col) == (b, a) or (columns is not None and col not in columns):
                    continue
                if group and (row, col) not in values and (row, col) not in problems:
                    values[row, col] = values[b, a]
                    derived[row, col] = address(b, a)
                else:
                    problems.setdefault((row, col), 'covered_merge')
    hidden = {row for row in sheet['hidden_rows'] if start <= row <= end}
    skipped = 0 if include_hidden else len(hidden)
    scope = {'sheet': sheet_name, 'start': region['start'], 'end': region['end'], 'verification': verification(region),
             'first_data_row': start if start <= end else None, 'last_data_row': end if start <= end else None,
             'populated_rows': len(populated), 'scanned_rows': max(0, end - start + 1) - skipped,
             'hidden_rows_excluded': skipped, 'includes_hidden_rows': include_hidden,
             'totals_rows_excluded': totals, 'totals_rows_basis': basis, 'merged_group_cells': len(derived),
             'uncertain_totals_rows': sorted(uncertain),
             'hidden_columns': [get_column_letter(col) for col in range(left, right + 1)
                                if any(item['start'] is not None and item['end'] is not None
                                       and item['start'] <= col <= item['end'] for item in sheet['hidden_column_ranges'])],
             'excluded_cells': sorted(key for key in sheet['excluded'] if contains(area, coordinate_to_tuple(key)))}
    return TableData(scope, left, right, values, problems, populated, set() if include_hidden else hidden, available_rows,
                     derived, notes, uncertain)


def join_candidates(raw, sheet, start, right_sheet, right_start):
    if sheet == right_sheet:
        raise ValueError('서로 다른 시트의 표 두 개를 지정해 주세요')
    if not isinstance(raw, dict):
        candidates, total = raw.candidate_page(sheet, start, right_sheet, right_start, 0, 5000)
        return candidates, total <= 5000
    # Recompute from original cells to apply current confidence and conflict rules to older published jobs.
    wanted = {(sheet, start), (right_sheet, right_start)}
    selected = [{**item, 'regions': [r for r in item['regions'] if (item['name'], r['start']) in wanted]}
                for item in raw['xlsx_sheets'] if item['name'] in {sheet, right_sheet}]
    candidates, complete = row_candidates(selected)
    for candidate in candidates:
        if candidate['left']['sheet'] != sheet:
            candidate['left'], candidate['right'] = candidate['right'], candidate['left']
            candidate['matching_rows'].reverse()
    return candidates, complete


def _join_rows(raw, left, right, join):
    candidates, complete = join_candidates(raw, left.scope['sheet'], left.scope['start'],
                                           right.scope['sheet'], right.scope['start'])
    if not complete:
        raise ValueError('연결 후보 한도를 넘었습니다. 중복 식별자를 정리해 주세요')
    eligible = {(item['left']['row'], item['right']['row']) for item in candidates if not item['ambiguous']}
    approved = {(item.left_row, item.right_row) for item in join.approved_pairs}
    if len(approved) != len(join.approved_pairs) or not approved <= eligible:
        raise ValueError('중복되거나 충돌하는 행 또는 원본과 일치하지 않는 연결을 승인할 수 없습니다')
    pairs = sorted((a, b) for a, b in approved if a not in left.hidden and b not in right.hidden)
    pending = sum((a, b) not in approved and a not in left.hidden and b not in right.hidden for a, b in eligible)
    ambiguous = sum(item['ambiguous'] and item['left']['row'] not in left.hidden
                    and item['right']['row'] not in right.hidden for item in candidates)
    details = {'right_scope': right.scope, 'approval': 'user_selected', 'candidate_pairs': len(candidates),
               'ambiguous_pairs': ambiguous, 'unapproved_pairs': pending,
               'hidden_pairs_excluded': len(approved) - len(pairs), 'joined_rows': len(pairs),
               'matched_pairs': [],
               'unmatched_left_rows': len(left.populated - left.hidden - {a for a, _ in pairs}),
               'unmatched_right_rows': len(right.populated - right.hidden - {b for _, b in pairs})}
    return pairs, details



def _join_key(value, kind, expected):
    if expected == 'text' and kind == 'string':
        return value if value.strip() else None
    if expected == 'number' and kind in {'integer', 'number'}:
        return Decimal(str(value))
    if expected == kind and kind in {'boolean', 'date', 'datetime'}:
        return value
    if expected == 'date' and kind == 'datetime':
        date_time = datetime.fromisoformat(value)
        return date_time.date().isoformat() if date_time.time() == time() else None
    return None


def _mapped_key_groups(table, columns):
    """Group certain data rows by key; possible totals rows are kept apart with their keys."""
    groups, uncertain = defaultdict(list), []
    rejected = Counter()
    for row in sorted(table.populated - table.hidden):
        values = []
        for column, expected in columns:
            value, kind, issue = table.raw_cell(row, column)
            converted = _join_key(value, kind, expected) if issue is None and value is not None else None
            if converted is None:
                if row not in table.uncertain_rows:
                    rejected[issue or ('blank_key' if value is None or (kind == 'string' and not value.strip())
                                       else 'key_type_mismatch')] += 1
                values = None
                break
            values.append(converted)
        if row in table.uncertain_rows:
            uncertain.append((row, tuple(values) if values is not None else None))
        elif values is not None:
            groups[tuple(values)].append(row)
    return groups, dict(rejected), uncertain


def mapped_rows(left, right, join):
    if left.scope['sheet'] == right.scope['sheet']:
        raise ValueError('서로 다른 시트의 표 두 개를 지정해 주세요')
    left_columns = [(left.column(key.left), key.type) for key in join.keys]
    right_columns = [(right.column(key.right), key.type) for key in join.keys]
    lhs, left_issues, left_uncertain = _mapped_key_groups(left, left_columns)
    rhs, right_issues, right_uncertain = _mapped_key_groups(right, right_columns)
    conflicts = {}
    for side, groups, uncertain, required, label in (
            ('left', lhs, left_uncertain, join.cardinality != 'many_to_one', '왼쪽'),
            ('right', rhs, right_uncertain, join.cardinality != 'one_to_many', '오른쪽')):
        duplicates = sum(len(rows) > 1 for rows in groups.values())
        if required and duplicates:
            raise ValueError(f'{label} 표의 고유 키가 중복됩니다 ({duplicates}개 키). 열 매핑과 관계 유형을 확인해 주세요')
        # A possible totals row never joins. When its key would break uniqueness,
        # report that the conflict comes from the unclear totals row.
        keys = Counter(key for _, key in uncertain if key is not None)
        conflicts[side] = sum(1 for _, key in uncertain if required and key is not None
                              and (key in groups or keys[key] > 1))
    pairs = sorted((a, b) for key in lhs.keys() & rhs.keys() for a in lhs[key] for b in rhs[key])
    left_out = {row for row, _ in left_uncertain}
    right_out = {row for row, _ in right_uncertain}
    details = {'right_scope': right.scope, 'approval': 'column_mapping', 'cardinality': join.cardinality,
               'keys': [key.model_dump() for key in join.keys], 'candidate_pairs': len(pairs),
               'ambiguous_pairs': 0, 'unapproved_pairs': 0, 'joined_rows': len(pairs), 'matched_pairs': [],
               'left_key_issues': left_issues, 'right_key_issues': right_issues,
               'uncertain_totals_rows': {'left': sorted(left_out), 'right': sorted(right_out)},
               'uncertain_key_conflicts': conflicts,
               'unmatched_left_rows': len(left.populated - left.hidden - left_out - {a for a, _ in pairs}),
               'unmatched_right_rows': len(right.populated - right.hidden - right_out - {b for _, b in pairs})}
    return pairs, details


def preview_mapping(raw, plan):
    join = plan.join
    left = _read_table(raw, plan.sheet, plan.start, plan.include_hidden_rows, {key.left for key in join.keys})
    right = _read_table(raw, join.sheet, join.start, plan.include_hidden_rows, {key.right for key in join.keys})
    pairs, details = mapped_rows(left, right, join)
    preview = []
    for a, b in pairs[plan.offset:plan.offset + 100]:
        points = {}
        for side, table, row in (('left', left, a), ('right', right, b)):
            columns = [table.column(getattr(key, side)) for key in join.keys]
            points[side] = {'sheet': table.scope['sheet'], 'row': row,
                            'cells': [address(row, col) for col in columns],
                            'values': [str(table.raw_cell(row, col)[0])[:160] for col in columns]}
        preview.append(points)
    return {'candidates': preview, 'total': len(pairs), 'offset': plan.offset,
            'scope': left.scope, 'join': details}


def _number_summary(operation, values):
    if not values:
        return {'value': None, 'numeric_values': 0}
    numbers = [Decimal(str(value)) for value in values]
    with localcontext() as context:
        # Cover the full exponent range of Excel binary64 values before rounding the result.
        context.prec = 650
        if operation == 'sum':
            result = sum(numbers)
        elif operation == 'average':
            result = sum(numbers) / len(numbers)
        else:
            result = (min if operation == 'min' else max)(numbers)
    # Decimal accumulation avoids binary cancellation. Use an exact decimal string only if JSON float overflows.
    if result == result.to_integral_value():
        value = int(result)
    else:
        number = float(result)
        value = number if math.isfinite(number) else str(result)
    return {'value': value, 'numeric_values': len(numbers)}


def execute(raw, plan):
    join = getattr(plan, 'join', None)

    def columns(side):
        if plan.operation == 'rows':
            return None
        needed = {rule.column for rule in plan.filters if rule.side == side}
        if plan.column and plan.column_side == side:
            needed.add(plan.column)
        if plan.group_by and plan.group_side == side:
            needed.add(plan.group_by)
        if isinstance(join, MappedJoin):
            needed.update(getattr(key, side) for key in join.keys)
        return needed

    left = _read_table(raw, plan.sheet, plan.start, plan.include_hidden_rows, columns('left'))
    tables = {'left': left}
    join_details = None
    if join:
        right = _read_table(raw, join.sheet, join.start, plan.include_hidden_rows, columns('right'))
        tables['right'] = right
        if isinstance(join, MappedJoin):
            pairs, join_details = mapped_rows(left, right, join)
        else:
            pairs, join_details = _join_rows(raw, left, right, join)
    else:
        pairs = [(row, None) for row in sorted(left.populated - left.hidden)]

    def column(side, key):
        if side not in tables:
            raise ValueError('오른쪽 표 열을 조회하려면 승인된 연결이 필요합니다')
        return side, tables[side].column(key)

    def read(pair, selector):
        side, col = selector
        return tables[side].get(pair[0 if side == 'left' else 1], col)

    def types(selector):
        return {kind for pair in pairs if (entry := read(pair, selector))[2] is None
                and (kind := entry[1]) != 'null'}

    target = column(plan.column_side, plan.column) if plan.column else None
    group = column(plan.group_side, plan.group_by) if plan.group_by else None
    filters = []
    for rule in plan.filters:
        selector = column(rule.side, rule.column)
        filters.append((selector, _prepare_filter(rule, types(selector))))
    if plan.operation == 'date_range' and (kinds := types(target)) and not kinds & {'date', 'datetime'}:
        raise ValueError('날짜 범위는 날짜 또는 일시 열에서 조회해 주세요')

    unit = plan.aggregate_unit
    if not join and unit in {'pairs', 'right_rows'}:
        raise ValueError('연결한 표가 있을 때 지정할 수 있는 집계 기준입니다')
    if join and plan.operation == 'rows' and unit in {'left_rows', 'right_rows'}:
        raise ValueError('행 목록은 연결된 행 쌍 기준으로 조회해 주세요')
    resolved = ('pairs' if unit in {'auto', 'pairs'} else unit) if join else 'rows'

    def evaluate(pair):
        outcome, notes = True, []
        for selector, predicate in filters:
            value, kind, issue = read(pair, selector)
            decision, note = (None, issue) if issue else predicate(value, kind)
            if decision is False:
                return False, []
            if decision is None:
                outcome = None
                notes.append(note or 'filter_type_mismatch')
        if any(row is not None and row in tables[side].uncertain_rows
               for side, row in zip(('left', 'right'), pair, strict=False) if side in tables):
            # A possible totals row is neither counted nor silently dropped.
            return None, [*notes, 'totals_row_uncertain']
        return outcome, notes

    # A unit is one aggregated item: a source row without a join, a joined pair,
    # or one source row with all of its joined pairs.
    units = []
    if resolved in {'rows', 'pairs'}:
        for pair in pairs:
            outcome, notes = evaluate(pair)
            units.append((pair, [pair] if outcome else [], outcome, notes))
    else:
        index = 0 if resolved == 'left_rows' else 1
        grouped = defaultdict(list)
        for pair in pairs:
            grouped[pair[index]].append(pair)
        for row in sorted(grouped):
            evaluated = [(pair, *evaluate(pair)) for pair in grouped[row]]
            passing = [pair for pair, outcome, _ in evaluated if outcome]
            undecided = [notes for _, outcome, notes in evaluated if outcome is None]
            outcome = True if passing else None if undecided else False
            units.append((row, passing, outcome, [note for notes in undecided for note in notes] if not passing else []))
    unit_side = 'right' if resolved == 'right_rows' else 'left'

    def unit_value(item, passing, selector):
        side, col = selector
        if resolved in {'rows', 'pairs'}:
            return read(item, selector), (side, item[0 if side == 'left' else 1])
        if side == unit_side:
            return tables[side].get(item, col), (side, item)
        others = {pair[0 if side == 'left' else 1] for pair in passing}
        if len(others) != 1:
            # One source row joined to several rows has no single value on the other side.
            return (None, 'null', 'multiple_join_values'), None
        other = next(iter(others))
        return tables[side].get(other, col), (side, other)

    matched, unknown, missing, blanks = 0, 0, 0, 0
    rows, temporal, numbers = [], [], []
    issues, distribution, group_counts, used = Counter(), Counter(), Counter(), Counter()
    group_numbers = defaultdict(list)
    for item, passing, outcome, notes in units:
        if outcome is False:
            continue
        if outcome is None:
            unknown += 1
            issues.update(notes)
            continue
        anchor = item if resolved in {'left_rows', 'right_rows'} else item[0]
        if plan.operation == 'count' and anchor not in tables[unit_side].available_rows:
            unknown += 1
            issues['unavailable_row'] += 1
            continue
        matched += 1
        if join_details:
            join_details['matched_pairs'].extend({'left_row': a, 'right_row': b} for a, b in passing)
        if plan.operation == 'rows' and plan.offset < matched <= plan.offset + plan.limit:
            record = left.record(item[0])
            entry = {'row': item[0], 'cells': record}
            issues.update(cell['issue'] for cell in record.values() if cell['issue'])
            if join_details:
                entry['right_row'], entry['right_cells'] = item[1], right.record(item[1])
                issues.update(cell['issue'] for cell in entry['right_cells'].values() if cell['issue'])
            rows.append(entry)
        group_key = None
        if group:
            (value, kind, issue), _ = unit_value(item, passing, group)
            if issue:
                issues[issue] += 1
            else:
                group_key = kind, value
                group_counts[group_key] += 1
        if not target:
            continue
        (value, kind, issue), source = unit_value(item, passing, target)
        if source and not issue and kind != 'null':
            used[source] += 1
        if issue:
            issues[issue] += 1
            missing += 1
        elif kind == 'null':
            blanks += 1
        elif plan.operation == 'distribution':
            distribution[kind, value] += 1
        elif plan.operation == 'date_range' and kind in {'date', 'datetime'}:
            temporal.append((datetime.fromisoformat(value), value))
        elif plan.operation in NUMERIC and kind == 'number':
            numbers.append(value)
            if group_key is not None:
                group_numbers[group_key].append(value)
        else:
            issues['non_numeric' if plan.operation in NUMERIC else 'non_date'] += 1
            missing += 1

    duplicated = sum(count > 1 for count in used.values())
    if duplicated and unit == 'auto' and plan.operation in DUPLICATE_SENSITIVE:
        # The same source value entered the aggregate through several joined pairs.
        # Choose 'pairs' to aggregate per relation or a row unit to count it once.
        issues['duplicated_join_values'] += duplicated
    for table in tables.values():
        issues.update(table.notes)
    if join_details:
        # Possible totals rows were kept out of the column-mapped join.
        for rows in (join_details.get('uncertain_totals_rows') or {}).values():
            if rows:
                issues['totals_row_uncertain'] += len(rows)
    result = {'matched_rows': matched, 'aggregate_unit': resolved}
    if plan.operation == 'distribution':
        buckets = distribution.most_common(plan.limit)
        result.update(distribution=[{'type': kind, 'value': value, 'count': count} for (kind, value), count in buckets],
                      omitted_buckets=len(distribution) - len(buckets))
    elif plan.operation == 'date_range':
        result.update(min=min(temporal)[1] if temporal else None, max=max(temporal)[1] if temporal else None)
    elif plan.operation == 'rows':
        result.update(rows=rows, offset=plan.offset, has_more=plan.offset + len(rows) < matched)
    elif plan.operation in NUMERIC:
        result.update(_number_summary(plan.operation, numbers))
    if group:
        groups = []
        for key, count in group_counts.most_common(plan.limit):
            kind, value = key
            entry = {'type': kind, 'group': value, 'matched_rows': count}
            if plan.operation in NUMERIC:
                entry.update(_number_summary(plan.operation, group_numbers[key]))
            groups.append(entry)
        result.update(groups=groups, omitted_groups=len(group_counts) - len(groups))
    if issues.get('duplicated_join_values'):
        # Without an explicit unit the repeated values make the figure ambiguous.
        # Keep the pair-based figure apart so it is not read as the answer.
        result['aggregate_unit_required'] = True
        for key in ('value', 'distribution'):
            if key in result:
                result[f'pair_based_{key}'] = result[key]
                result[key] = None
        for entry in result.get('groups', []):
            if 'value' in entry:
                entry['pair_based_value'], entry['value'] = entry['value'], None

    return {'operation': plan.operation, 'result': result, 'complete': not (
                unknown or missing or issues or result.get('omitted_buckets') or result.get('omitted_groups')
                or result.get('has_more') or (plan.operation == 'rows' and plan.offset)
                or (join_details and (join_details['ambiguous_pairs'] or join_details['unapproved_pairs']
                                      or join_details.get('left_key_issues') or join_details.get('right_key_issues')))),
            'scope': left.scope, 'join': join_details,
            'unknown_filter_rows': unknown, 'unavailable_values': missing, 'blank_values': blanks, 'issues': dict(issues)}


def unavailable():
    return IndexerError('XLSX_QUERY_UNAVAILABLE', '게시된 XLSX 구조를 확인할 수 없습니다. 직접 추출한 작업을 게시해 주세요.', 409)
