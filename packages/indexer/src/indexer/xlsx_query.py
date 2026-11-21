"""Evaluate bounded, typed query plans over complete verified XLSX table ranges."""

import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, localcontext
from typing import Literal
from uuid import UUID

from openpyxl.utils import column_index_from_string, get_column_letter
from openpyxl.utils.cell import coordinate_to_tuple, range_boundaries
from pydantic import BaseModel, ConfigDict, Field, model_validator

from indexer.contracts import IndexerError
from indexer.xlsx_structure import (
    STRUCTURE_VERSION,
    address,
    confirmed,
    contains,
    occupied,
    row_candidates,
    verification,
)

Scalar = str | bool | int | float
NUMERIC = {'sum', 'average', 'min', 'max'}
COLUMN = r'^[A-Z]{1,3}$'
TABLE_START = r'^[A-Z]{1,3}[1-9][0-9]{0,6}$'
Side = Literal['left', 'right']


class TableFilter(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)
    column: str = Field(pattern=COLUMN, max_length=3)
    side: Side = 'left'
    operator: Literal['eq', 'ne', 'lt', 'lte', 'gt', 'gte', 'contains', 'is_blank', 'not_blank']
    value: Scalar | None = None

    @model_validator(mode='after')
    def check_value(self):
        if (self.operator in {'is_blank', 'not_blank'}) != (self.value is None):
            raise ValueError('필터 연산자에 맞는 값을 지정해 주세요')
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


class PublishedTableQuery(QueryPlan):
    document_id: UUID = Field(strict=False)
    index_version: UUID | None = Field(default=None, strict=False)
    profile_id: str | None = Field(default=None, min_length=1, max_length=64)
    join: ApprovedJoin | None = None

    @model_validator(mode='after')
    def pin_approval(self):
        if self.join and not self.index_version:
            raise ValueError('행 연결을 승인한 색인 버전을 지정해 주세요')
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


def _prepare_filter(rule, types):
    value = rule.value
    relation = rule.operator
    if relation in {'is_blank', 'not_blank'}:
        return lambda cell_value, _: (cell_value is None) == (relation == 'is_blank')
    if relation == 'contains' and (not isinstance(value, str) or 'string' not in types):
        raise ValueError('포함 필터에는 문자열 열과 값이 필요합니다')
    if isinstance(value, bool) and relation not in {'eq', 'ne'}:
        raise ValueError('참·거짓 값에는 같음과 다름을 사용할 수 있습니다')
    convert = {}
    for kind in types:
        if relation == 'contains' and kind != 'string':
            continue
        if kind == 'boolean' and isinstance(value, bool) or kind == 'string' and isinstance(value, str) or kind == 'number' and type(value) in {float, int} and (type(value) is int or math.isfinite(value)):
            convert[kind] = value
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
        if kind not in convert:
            return False if kind == 'null' else None
        expected = convert[kind]
        actual = TEMPORAL[kind].fromisoformat(cell_value) if kind in TEMPORAL else cell_value
        if relation == 'contains':
            return expected.casefold() in actual.casefold()
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

    return matches


def verified_tables(raw):
    if raw.get('xlsx_structure_version') != STRUCTURE_VERSION:
        return []
    catalog = []
    for sheet in raw['xlsx_sheets']:
        if not sheet['regions']:
            continue
        cells = occupied(sheet)
        for region in sheet['regions']:
            if region['kind'] == 'table' and confirmed(region):
                left, _, right, _ = range_boundaries(f"{region['start']}:{region['end']}")
                columns = {get_column_letter(col): None for col in range(left, right + 1)}
                for key in region['headers']:
                    columns[get_column_letter(coordinate_to_tuple(key)[1])] = cells[key]['display_text']
                catalog.append({'sheet': sheet['name'], 'start': region['start'], 'end': region['end'],
                                'title': cells[region['title']]['display_text'] if region['title'] else region['table_name'],
                                'columns': columns, 'verification': verification(region)})
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

    def column(self, key):
        number = column_index_from_string(key)
        if not self.left <= number <= self.right:
            raise ValueError('표 범위에 속한 열을 지정해 주세요')
        return number

    def get(self, row, column):
        key = address(row, column)
        return *self.values.get(key, (None, 'null')), self.problems.get(key)

    def record(self, row):
        record = {}
        for col in range(self.left, self.right + 1):
            value, kind, issue = self.get(row, col)
            record[get_column_letter(col)] = {'value': value if issue is None else None, 'type': kind,
                                              'source': address(row, col), 'issue': issue}
        return record


def _read_table(raw, sheet_name, region_start, include_hidden):
    sheet = next((item for item in raw['xlsx_sheets'] if item['name'] == sheet_name), None)
    region = next((r for r in sheet['regions'] if r['start'] == region_start and r['kind'] == 'table'
                   and confirmed(r)), None) if sheet else None
    if not region:
        raise ValueError('Excel에 등록된 표 또는 사용자가 확인한 표를 지정해 주세요')
    area = range_boundaries(f"{region['start']}:{region['end']}")
    left, top, right, bottom = area
    start = max((coordinate_to_tuple(key)[0] + 1 for key in region['headers']), default=top)
    if region['title'] and coordinate_to_tuple(region['title'])[0] == start:
        start += 1
    end = bottom - region['totals_rows']
    cells = {key: cell for key, cell in occupied(sheet).items() if contains(area, coordinate_to_tuple(key))}
    values, problems = {}, {}
    populated = set()
    for key, cell in cells.items():
        row, _ = coordinate_to_tuple(key)
        if not start <= row <= end or key == region['title']:
            continue
        populated.add(row)
        if key in sheet['excluded']:
            problems[key] = 'excluded_cell'
        elif cell['hidden_merged'] or cell.get('cache_status') == 'missing':
            problems[key] = 'hidden_merged' if cell['hidden_merged'] else 'missing_cache'
        else:
            value, value_type = _value(cell)
            values[key] = value, _kind(value_type)
            if value_type == 'error':
                problems[key] = 'excel_error'
    for ref in sheet['merged_ranges']:
        a, b, c, d = range_boundaries(ref)
        if contains(area, (b, a)):
            for row in range(max(start, b), min(d, end) + 1):
                for col in range(a, c + 1):
                    if (row, col) != (b, a):
                        problems.setdefault(address(row, col), 'covered_merge')
    hidden = {row for row in sheet['hidden_rows'] if start <= row <= end}
    skipped = 0 if include_hidden else len(hidden)
    scope = {'sheet': sheet_name, 'start': region['start'], 'end': region['end'], 'verification': verification(region),
             'first_data_row': start if start <= end else None, 'last_data_row': end if start <= end else None,
             'populated_rows': len(populated), 'scanned_rows': max(0, end - start + 1) - skipped,
             'hidden_rows_excluded': skipped, 'includes_hidden_rows': include_hidden,
             'hidden_columns': [get_column_letter(col) for col in range(left, right + 1)
                                if any(item['start'] is not None and item['end'] is not None
                                       and item['start'] <= col <= item['end'] for item in sheet['hidden_column_ranges'])],
             'excluded_cells': sorted(key for key in sheet['excluded'] if key in cells)}
    return TableData(scope, left, right, values, problems, populated, set() if include_hidden else hidden)


def join_candidates(raw, sheet, start, right_sheet, right_start):
    if sheet == right_sheet:
        raise ValueError('서로 다른 시트의 표 두 개를 지정해 주세요')
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
    left = _read_table(raw, plan.sheet, plan.start, plan.include_hidden_rows)
    tables = {'left': left}
    join_details = None
    if join := getattr(plan, 'join', None):
        right = _read_table(raw, join.sheet, join.start, plan.include_hidden_rows)
        tables['right'] = right
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

    matched, unknown, missing, blanks = 0, 0, 0, 0
    rows, temporal, numbers = [], [], []
    issues, distribution, group_counts = Counter(), Counter(), Counter()
    group_numbers = defaultdict(list)
    populated_left = {coordinate_to_tuple(key)[0] for key in left.values if key not in left.problems}
    for pair in pairs:
        evaluations = []
        for selector, predicate in filters:
            value, kind, issue = read(pair, selector)
            decision = None if issue else predicate(value, kind)
            evaluations.append((decision, issue or 'filter_type_mismatch'))
        if any(decision is False for decision, _ in evaluations):
            continue
        if any(decision is None for decision, _ in evaluations):
            unknown += 1
            issues.update(issue for decision, issue in evaluations if decision is None)
            continue
        if plan.operation == 'count' and pair[0] not in populated_left:
            unknown += 1
            issues['unavailable_row'] += 1
            continue
        matched += 1
        if join_details:
            join_details['matched_pairs'].append({'left_row': pair[0], 'right_row': pair[1]})
        if plan.operation == 'rows' and plan.offset < matched <= plan.offset + plan.limit:
            record = left.record(pair[0])
            entry = {'row': pair[0], 'cells': record}
            issues.update(cell['issue'] for cell in record.values() if cell['issue'])
            if join_details:
                entry['right_row'], entry['right_cells'] = pair[1], right.record(pair[1])
                issues.update(cell['issue'] for cell in entry['right_cells'].values() if cell['issue'])
            rows.append(entry)
        group_key = None
        if group:
            value, kind, issue = read(pair, group)
            if issue:
                issues[issue] += 1
            else:
                group_key = kind, value
                group_counts[group_key] += 1
        if not target:
            continue
        value, kind, issue = read(pair, target)
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

    result = {'matched_rows': matched}
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

    return {'operation': plan.operation, 'result': result, 'complete': not (
                unknown or missing or issues or result.get('omitted_buckets') or result.get('omitted_groups')
                or result.get('has_more') or (plan.operation == 'rows' and plan.offset)
                or (join_details and (join_details['ambiguous_pairs'] or join_details['unapproved_pairs']))),
            'scope': left.scope, 'join': join_details,
            'unknown_filter_rows': unknown, 'unavailable_values': missing, 'blank_values': blanks, 'issues': dict(issues)}


def unavailable():
    return IndexerError('XLSX_QUERY_UNAVAILABLE', '게시된 XLSX 구조를 확인할 수 없습니다. 직접 추출한 작업을 게시해 주세요.', 409)
