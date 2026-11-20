from indexer.chunking import normalize_elements
from indexer.content_comparison import compare


def elements(html, text="본문 교육 계획"):
    return normalize_elements([
        {"page": 1, "category": "paragraph", "content": {"text": text}},
        {"page": 1, "category": "table", "content": {"html": html}},
    ])


def test_detects_text_and_repeated_cell_loss_but_not_spacing_or_case():
    previous = elements('<table><tr><td>교육</td><td>교육</td></tr></table>')
    same = elements('<table><tr><td>교육</td><td>교육</td></tr></table>', '본문  교육 계획')
    assert compare(previous, same) == []
    changed = elements('<table><tr><td>교육</td></tr></table>', '본문 계획')
    assert compare(previous, changed) == [{"page": 1, "missing_word_count": 1, "missing_cell_count": 1, "changed_table_row_count": 1}]


def test_detects_changed_row_relation_even_if_all_cells_remain():
    previous = elements('<table><tr><td>교육</td><td>10</td></tr><tr><td>연구</td><td>20</td></tr></table>')
    changed = elements('<table><tr><td>교육</td><td>20</td></tr><tr><td>연구</td><td>10</td></tr></table>')
    assert compare(previous, changed)[0]["changed_table_row_count"] == 2
    assert compare(previous, changed)[0]["missing_cell_count"] == 0
