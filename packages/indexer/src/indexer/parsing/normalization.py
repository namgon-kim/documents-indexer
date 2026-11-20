from dataclasses import dataclass

BLOCK_TAGS = {"p", "li", "div", "caption", "figcaption", "td", "th", "br", "pre"}
FIGURES = {"figure", "chart"}
PAGE_FURNITURE = {"header", "footer", "page_header", "page_footer", "page_number"}


def clean(text):
    return " ".join(text.split())


def readable(tag):
    for node in tag.find_all("br"):
        node.replace_with(" ")
    for node in tag.find_all(BLOCK_TAGS - {"br"}):
        node.insert_before(" ")
        node.insert_after(" ")
    return clean(tag.get_text())


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


