"""A rebuildable, indexed projection of one native XLSX parsing artifact."""

import fcntl
import json
import os
import sqlite3
import tempfile
from contextlib import closing, contextmanager
from pathlib import Path
from uuid import UUID

from openpyxl.utils.cell import coordinate_to_tuple

from indexer.xlsx_query import typed_table_cell, verified_tables
from indexer.xlsx_review import cell_projection, sheet_detail, sheet_summary, window_response
from indexer.xlsx_structure import STRUCTURE_VERSION, identity_rows, occupied

INDEX_VERSION = 3
CANDIDATE_LIMIT = 5000


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _fingerprint(status):
    return INDEX_VERSION, status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns


def _build(source, destination, fingerprint, raw=None):
    if raw is None:
        with source.open(encoding='utf-8') as stream:
            if _fingerprint(os.fstat(stream.fileno())) != fingerprint:
                return False
            raw = json.load(stream)
    if raw.get('xlsx_structure_version') != STRUCTURE_VERSION:
        raise ValueError('문서를 다시 읽은 뒤 XLSX 구조를 검토해 주세요.')
    with closing(sqlite3.connect(destination)) as db, db:
        db.executescript("""
            CREATE TABLE metadata (fingerprint TEXT, catalog TEXT);
            CREATE TABLE sheets (name TEXT PRIMARY KEY, ordinal INTEGER, summary TEXT, detail TEXT);
            CREATE TABLE cells (
                sheet TEXT, row INTEGER, col INTEGER, value TEXT, kind TEXT, issue TEXT, preview TEXT, payload TEXT,
                PRIMARY KEY (sheet, col, row)
            ) WITHOUT ROWID;
            CREATE INDEX cell_availability ON cells (sheet, col, row, issue);
            CREATE TABLE identities (
                sheet TEXT, start TEXT, identifier TEXT, row INTEGER, name TEXT, point TEXT,
                PRIMARY KEY (sheet, start, identifier, row)
            ) WITHOUT ROWID;
        """)
        db.execute('INSERT INTO metadata VALUES (?, ?)', (_json(fingerprint), _json(verified_tables(raw))))
        for ordinal, sheet in enumerate(raw['xlsx_sheets']):
            name = sheet['name']
            summary = sheet_summary(sheet)
            detail = {**sheet_detail(sheet), 'hidden_rows': sheet['hidden_rows'],
                      'hidden_column_ranges': sheet['hidden_column_ranges']}
            db.execute('INSERT INTO sheets VALUES (?, ?, ?, ?)', (name, ordinal, _json(summary), _json(detail)))
            cells = occupied(sheet)

            db.executemany('INSERT INTO cells VALUES (?, ?, ?, ?, ?, ?, ?, ?)', _cell_records(sheet, cells))
            db.executemany('INSERT INTO identities VALUES (?, ?, ?, ?, ?, ?)', _identity_records(sheet, cells))
    return True



def _cell_records(sheet, cells):
    for key, cell in cells.items():
        row, col = coordinate_to_tuple(key)
        value, kind, issue = typed_table_cell(cell, key in sheet['excluded'])
        yield sheet['name'], row, col, _json(value), kind, issue, _json(cell_projection(cell)), _json(cell)


def _identity_records(sheet, cells):
    for identifier, point in identity_rows(sheet, cells):
        name_key = point.pop('name_key')
        point['values'] = [cells[key]['display_text'][:160] for key in point['cells']]
        yield sheet['name'], point['region_start'], identifier, point['row'], name_key, _json(point)


def _read_index(path, fingerprint):
    if not path.is_file():
        return None
    db = None
    try:
        db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
        if db.execute('SELECT fingerprint FROM metadata').fetchone() == (_json(fingerprint),):
            return db
    except sqlite3.DatabaseError:
        # The JSON artifact can recreate an outdated or damaged index.
        pass
    if db is not None:
        db.close()
    return None


@contextmanager
def open_workbook(storage, document_id, job_id, *, parsed=None, source_stat=None):
    folder = storage.document_dir(document_id) / 'jobs' / str(UUID(job_id))
    source = folder / 'parser_result.json'
    database = folder / 'xlsx-access.sqlite3'
    db = _read_index(database, _fingerprint(source.stat()))
    if db is None:
        with (folder / 'xlsx-access.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            for _ in range(3):
                fingerprint = _fingerprint(source.stat())
                db = _read_index(database, fingerprint)
                if db is not None:
                    break
                fd, filename = tempfile.mkstemp(dir=folder, prefix='.xlsx-access-')
                os.close(fd)
                temporary = Path(filename)
                try:
                    original = parsed if source_stat is not None and _fingerprint(source_stat) == fingerprint else None
                    if _build(source, temporary, fingerprint, original) and _fingerprint(source.stat()) == fingerprint:
                        os.replace(temporary, database)
                        db = _read_index(database, fingerprint)
                        if db is not None:
                            break
                except sqlite3.Error as error:
                    raise OSError('XLSX 조회 색인을 만들 수 없습니다.') from error
                finally:
                    temporary.unlink(missing_ok=True)
            if db is None:
                raise OSError('XLSX 산출물이 변경되어 조회를 다시 시도해야 합니다.')
    with closing(db):
        yield WorkbookAccess(db)


class WorkbookAccess:
    def __init__(self, db):
        self.db = db

    def catalog(self):
        return json.loads(self.db.execute('SELECT catalog FROM metadata').fetchone()[0])

    def summaries(self):
        return [json.loads(row[0]) for row in self.db.execute('SELECT summary FROM sheets ORDER BY ordinal')]

    def sheet(self, name):
        row = self.db.execute('SELECT detail FROM sheets WHERE name = ?', (name,)).fetchone()
        return json.loads(row[0]) if row else None

    def _range(self, name, area, query, columns=None):
        left, top, right, bottom = area
        if left > right or top > bottom:
            return
        for col in range(left, right + 1) if columns is None else sorted(columns):
            yield from self.db.execute(query, (name, col, top, bottom))

    def table_cells(self, name, area, columns=None):
        for row, col, value, kind, issue in self._range(name, area,
                'SELECT row, col, value, kind, issue FROM cells WHERE sheet = ? AND col = ? AND row BETWEEN ? AND ?', columns):
            yield row, col, json.loads(value), kind, issue

    def row_availability(self, name, area):
        left, top, right, bottom = area
        return self.db.execute("""
            SELECT row, MAX(issue IS NULL) FROM cells INDEXED BY cell_availability
            WHERE sheet = ? AND col BETWEEN ? AND ? AND row BETWEEN ? AND ? GROUP BY row
        """, (name, left, right, top, bottom))

    def cell(self, sheet, address):
        row, col = coordinate_to_tuple(address)
        result = self.db.execute('SELECT payload FROM cells WHERE sheet = ? AND col = ? AND row = ?',
                                 (sheet, col, row)).fetchone()
        return json.loads(result[0]) if result else None

    def window(self, sheet, row, column, rows, columns, *, exact=None):
        if exact:
            cell = self.cell(sheet['name'], exact)
            names = {item['name'].casefold(): item['name'] for item in self.summaries()} if cell else {}
            cells = [cell_projection(cell, sheet['name'], names, exact=True)] if cell else []
        else:
            area = (column, row, column + columns - 1, row + rows - 1)
            records = self._range(sheet['name'], area,
                'SELECT row, col, preview FROM cells WHERE sheet = ? AND col = ? AND row BETWEEN ? AND ?')
            cells = [json.loads(preview) for _, _, preview in sorted(records)]
        return window_response(sheet, row, column, rows, columns, cells)

    def candidate_page(self, sheet, start, right_sheet, right_start, offset, limit):
        args = (sheet, start, right_sheet, right_start)
        counts = """
            WITH l AS (
                SELECT identifier, COUNT(*) AS n FROM identities WHERE sheet = ? AND start = ? GROUP BY identifier
            ), r AS (
                SELECT identifier, COUNT(*) AS n FROM identities WHERE sheet = ? AND start = ? GROUP BY identifier
            )
        """
        groups = self.db.execute(counts + 'SELECT identifier, l.n, r.n FROM l JOIN r USING (identifier) ORDER BY identifier',
                                 args).fetchall()
        total = sum(nl * nr for _, nl, nr in groups)
        # Skip duplicate groups by cardinality before fetching points for this page.
        remaining = max(0, min(limit, CANDIDATE_LIMIT - offset, total - offset))
        candidates = []
        point_query = """
            SELECT name, point FROM identities WHERE sheet = ? AND start = ? AND identifier = ?
            ORDER BY row LIMIT ? OFFSET ?
        """
        for identifier, nl, nr in groups:
            if not remaining:
                break
            if offset >= nl * nr:
                offset -= nl * nr
                continue
            left_start, right_start_row = divmod(offset, nr)
            size = min(remaining, nl * nr - offset)
            left_size = (right_start_row + size + nr - 1) // nr
            left_points = self.db.execute(point_query, (sheet, start, identifier, left_size, left_start))
            skip = right_start_row
            for lname, left in left_points:
                right_points = self.db.execute(point_query, (right_sheet, right_start, identifier, size, skip))
                for rname, right in right_points:
                    same = lname == rname
                    candidates.append({'left': json.loads(left), 'right': json.loads(right), 'status': 'unverified',
                                       'conflict': not same, 'ambiguous': not same or nl != 1 or nr != 1,
                                       'matching_rows': [nl, nr], 'evidence': ['id', 'name'] if same else ['id']})
                    size -= 1
                    remaining -= 1
                skip = 0
            offset = 0
        return candidates, total
