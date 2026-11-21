"""Evaluate bounded, typed query plans over complete verified XLSX table ranges."""

import math
from collections import Counter
from datetime import date, datetime, time
from typing import Literal
from uuid import UUID

from openpyxl.utils import column_index_from_string, get_column_letter
from openpyxl.utils.cell import coordinate_to_tuple, range_boundaries
from pydantic import BaseModel, ConfigDict, Field, model_validator

from indexer.contracts import IndexerError
from indexer.xlsx_structure import STRUCTURE_VERSION, address, contains, occupied

Scalar = str | bool | int | float


class TableFilter(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)
    column: str = Field(pattern=r'^[A-Z]{1,3}$', max_length=3)
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
    start: str = Field(pattern=r'^[A-Z]{1,3}[1-9][0-9]{0,6}$', max_length=10)
    operation: Literal['count', 'distribution', 'date_range', 'rows']
    column: str | None = Field(default=None, pattern=r'^[A-Z]{1,3}$', max_length=3)
    filters: list[TableFilter] = Field(default_factory=list, max_length=20)
    include_hidden_rows: bool = True
    offset: int = Field(default=0, ge=0, le=5000)
    limit: int = Field(default=100, ge=1, le=100)

    @model_validator(mode='after')
    def check_column(self):
        if (self.operation in {'distribution', 'date_range'}) != (self.column is not None):
            raise ValueError('집계 연산에 맞는 열을 지정해 주세요')
        return self


class PublishedTableQuery(QueryPlan):
    document_id: UUID = Field(strict=False)
    index_version: UUID | None = Field(default=None, strict=False)
    profile_id: str | None = Field(default=None, min_length=1, max_length=64)


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
            if region['kind'] == 'table' and region['status'] == 'verified':
                left, _, right, _ = range_boundaries(f"{region['start']}:{region['end']}")
                columns = {get_column_letter(col): None for col in range(left, right + 1)}
                for key in region['headers']:
                    columns[get_column_letter(coordinate_to_tuple(key)[1])] = cells[key]['display_text']
                catalog.append({'sheet': sheet['name'], 'start': region['start'], 'end': region['end'],
                                'title': cells[region['title']]['display_text'] if region['title'] else region['table_name'],
                                'columns': columns})
    return catalog


def execute(raw, plan):
    sheet = next((item for item in raw['xlsx_sheets'] if item['name'] == plan.sheet), None)
    region = next((r for r in sheet['regions'] if r['start'] == plan.start and r['kind'] == 'table'
                   and r['status'] == 'verified'), None) if sheet else None
    if not region:
        raise ValueError('검증된 표에서 조회할 범위를 지정해 주세요')
    area = range_boundaries(f"{region['start']}:{region['end']}")
    left, top, right, bottom = area
    selected_columns = {key: column_index_from_string(key) for key in
                        {rule.column for rule in plan.filters} | ({plan.column} if plan.column else set())}
    if any(not left <= col <= right for col in selected_columns.values()):
        raise ValueError('표 범위에 속한 열을 지정해 주세요')
    start = max((coordinate_to_tuple(key)[0] + 1 for key in region['headers']), default=top)
    if region['title'] and coordinate_to_tuple(region['title'])[0] == start:
        start += 1
    end = bottom - region['totals_rows']
    cells = {key: cell for key, cell in occupied(sheet).items() if contains(area, coordinate_to_tuple(key))}
    values = {}
    populated, problems = set(), {}
    for key, cell in cells.items():
        row, _ = coordinate_to_tuple(key)
        if start <= row <= end and key != region['title']:
            populated.add(row)
            if key in sheet['excluded']:
                problems[key] = 'excluded_cell'
            elif cell['hidden_merged'] or cell.get('cache_status') == 'missing':
                problems[key] = 'hidden_merged' if cell['hidden_merged'] else 'missing_cache'
            elif not cell['hidden_merged']:
                value, value_type = _value(cell)
                values[key] = value, _kind(value_type)
                if value_type == 'error':
                    problems[key] = 'excel_error'
    for ref in sheet['merged_ranges']:
        a, b, c, d = range_boundaries(ref)
        if not contains(area, (b, a)):
            continue
        for row in range(max(start, b), min(d, end) + 1):
            for col in range(a, c + 1):
                if (row, col) != (b, a):
                    problems.setdefault(address(row, col), 'covered_merge')
    hidden = {row for row in sheet['hidden_rows'] if start <= row <= end}
    types_by_col = {name: {kind for row in populated
                          if (key := address(row, col)) in values and key not in problems
                          and (kind := values[key][1]) != 'null'} for name, col in selected_columns.items()}
    filters = [(selected_columns[rule.column], _prepare_filter(rule, types_by_col[rule.column]))
               for rule in plan.filters]
    if plan.operation == 'date_range' and types_by_col[plan.column] and not (types_by_col[plan.column] & {'date', 'datetime'}):
        raise ValueError('날짜 범위는 날짜 또는 일시 열에서 조회해 주세요')

    matched, rows, issues = [], [], Counter()
    distribution, temporal = Counter(), []
    skipped_hidden = 0 if plan.include_hidden_rows else len(hidden)
    unknown_matches = missing_values = blank_target = 0
    for row in sorted(populated):
        if row in hidden and not plan.include_hidden_rows:
            continue
        evaluations = []
        for col, predicate in filters:
            key = address(row, col)
            evaluations.append(None if key in problems else predicate(*values.get(key, (None, 'null'))))
        if False in evaluations:
            continue
        if None in evaluations:
            unknown_matches += 1
            issues.update(problems.get(address(row, col), 'filter_type_mismatch')
                          for (col, _), decision in zip(filters, evaluations, strict=True) if decision is None)
            continue
        matched.append(row)
        if plan.operation == 'count' and all(address(row, col) in problems or address(row, col) not in values
                                              for col in range(left, right + 1)):
            matched.pop()
            unknown_matches += 1
            issues['unavailable_row'] += 1
            continue
        if plan.operation == 'rows' and plan.offset < len(matched) <= plan.offset + plan.limit:
            record = {}
            for col in range(left, right + 1):
                key = address(row, col)
                value, kind = values.get(key, (None, 'null'))
                record[get_column_letter(col)] = {'value': value if key not in problems else None,
                                                  'type': kind, 'source': key, 'issue': problems.get(key)}
                if key in problems:
                    issues[problems[key]] += 1
            rows.append({'row': row, 'cells': record})
        if plan.column:
            key = address(row, selected_columns[plan.column])
            value, kind = values.get(key, (None, 'null'))
            if key in problems:
                issues[problems[key]] += 1
                missing_values += 1
            elif kind == 'null':
                blank_target += 1
            elif plan.operation == 'distribution':
                distribution[kind, value] += 1
            elif kind in {'date', 'datetime'}:
                normalized = datetime.fromisoformat(value)
                temporal.append((normalized, value))
            else:
                missing_values += 1
                issues['non_date'] += 1

    result = {'matched_rows': len(matched)}
    if plan.operation == 'distribution':
        buckets = distribution.most_common(plan.limit)
        result.update(distribution=[{'type': kind, 'value': value, 'count': count} for (kind, value), count in buckets],
                      omitted_buckets=len(distribution) - len(buckets))
    elif plan.operation == 'date_range':
        result.update(min=min(temporal)[1] if temporal else None, max=max(temporal)[1] if temporal else None)
    elif plan.operation == 'rows':
        result.update(rows=rows, offset=plan.offset, has_more=plan.offset + len(rows) < len(matched))
    return {'operation': plan.operation, 'result': result, 'complete': not (unknown_matches or missing_values or issues
                or result.get('omitted_buckets') or result.get('has_more') or (plan.operation == 'rows' and plan.offset)),
            'scope': {'sheet': sheet['name'], 'start': region['start'], 'end': region['end'],
                      'first_data_row': start if start <= end else None, 'last_data_row': end if start <= end else None,
                      'populated_rows': len(populated), 'scanned_rows': max(0, end - start + 1) - skipped_hidden,
                      'hidden_rows_excluded': skipped_hidden, 'includes_hidden_rows': plan.include_hidden_rows,
                      'hidden_columns': [get_column_letter(col) for col in range(left, right + 1)
                                         if any(item['start'] is not None and item['end'] is not None
                                                and item['start'] <= col <= item['end']
                                                for item in sheet['hidden_column_ranges'])],
                      'excluded_cells': sorted(key for key in sheet['excluded'] if key in cells)},
            'unknown_filter_rows': unknown_matches, 'unavailable_values': missing_values, 'blank_values': blank_target,
            'issues': dict(issues)}


def unavailable():
    return IndexerError('XLSX_QUERY_UNAVAILABLE', '게시된 XLSX 구조를 확인할 수 없습니다. 직접 추출한 작업을 게시해 주세요.', 409)
