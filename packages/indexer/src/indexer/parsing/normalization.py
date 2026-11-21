import re
from dataclasses import dataclass
from functools import cached_property

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

BLOCK_TAGS = {"p", "li", "ul", "ol", "div", "caption", "figcaption", "table", "tr", "td", "th", "pre",
              "blockquote", "section", "h1", "h2", "h3", "h4", "h5", "h6"}
FIGURES = {"figure", "chart"}
PAGE_FURNITURE = {"header", "footer", "page_header", "page_footer", "page_number"}
NON_CONTENT = {"script", "style", "iframe", "object", "template"}
WHITE_SPACE = re.compile(r"(?:^|;)\s*white-space\s*:\s*(pre-wrap|break-spaces|pre)\s*(?:!important\s*)?(?:;|$)", re.IGNORECASE)


def clean(text):
    return " ".join(text.split())

def parse_table_html(html):
    # The default parser collapses whitespace-only nodes around inline tags.
    return BeautifulSoup(html, 'html.parser', preserve_whitespace_tags={'pre', 'textarea', 'td', 'th'})


def _preserves_whitespace(node):
    return node.name in {'pre', 'code'} or WHITE_SPACE.search(node.get('style', '')) is not None


def readable(tag):
    """Read source layout without mutating the HTML or folding cell whitespace."""
    parts = []
    block_break = False
    inline_space = ''

    stack = [(tag, any(_preserves_whitespace(parent) for parent in tag.parents), False)]
    while stack:
        node, preserve, close_block = stack.pop()
        if close_block:
            block_break, inline_space = True, ''
            continue
        if isinstance(node, Comment):
            continue
        if isinstance(node, NavigableString):
            value = str(node).replace('\r\n', '\n').replace('\r', '\n')
            if not preserve:
                if not value.strip():
                    if parts and not block_break and not parts[-1].endswith('\n'):
                        inline_space += ' ' if '\n' in value else value
                    continue
                # Ignore the wrapper's blank lines. Keep line breaks inside text.
                value = re.sub(r'^(?:[ \t]*\n)+|(?:\n[ \t]*)+$', '', value)
            if block_break:
                if parts and not parts[-1].endswith('\n'):
                    parts.append('\n')
            elif inline_space:
                parts.append(inline_space)
            block_break, inline_space = False, ''
            parts.append(value)
            continue
        if not isinstance(node, Tag) or node.name in NON_CONTENT:
            continue
        if node.name == 'br':
            parts.append('\n')
            block_break, inline_space = False, ''
            continue
        if node.name in BLOCK_TAGS:
            block_break, inline_space = True, ''
            stack.append((node, preserve, True))
        keep_space = preserve or _preserves_whitespace(node)
        stack.extend((child, keep_space, False) for child in reversed(node.contents))

    return ''.join(parts)


def image_alternatives(soup):
    return dict.fromkeys(clean(image.get("alt", "")) for image in soup.find_all("img"))


def list_items_from(soup):
    items = []
    paths = {}
    for node in soup.find_all("li"):
        parent_path = paths.get(id(node.find_parent("li")), [])
        text = clean("".join(str(word) for word in node.strings if word.find_parent("li") is node))
        paths[id(node)] = [*parent_path, text] if text else parent_path
        if text:
            items.append({"ancestors": parent_path, "text": text})
    return items


@dataclass(frozen=True)
class Cell:
    row: int
    column: int
    rows: int
    columns: int
    text: str
    header: bool
    scope: str

    @cached_property
    def search_text(self):
        return clean(self.text)


def cells_from(table):
    rows = [row for row in table.find_all("tr") if row.find_parent("table") is table]
    if not rows or len(rows) > 5000:
        return []
    occupied = set()
    cells = []
    for row_index, row in enumerate(rows):
        column = 0
        for node in row.find_all(["td", "th"], recursive=False):
            while (row_index, column) in occupied:
                column += 1
            try:
                rowspan = int(node.get("rowspan", 1))
                colspan = int(node.get("colspan", 1))
            except ValueError:
                return []
            if not 1 <= rowspan <= len(rows) - row_index or not 1 <= colspan <= 100 - column:
                return []
            positions = {
                (r, c) for r in range(row_index, row_index + rowspan) for c in range(column, column + colspan)
            }
            if occupied & positions or len(occupied) + len(positions) > 25_000:
                return []
            occupied |= positions
            cells.append(
                Cell(
                    row_index,
                    column,
                    rowspan,
                    colspan,
                    readable(node),
                    node.name == "th",
                    str(node.get("scope", "")),
                )
            )
            column += colspan
    return cells


