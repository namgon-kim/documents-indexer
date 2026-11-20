from indexer.parse_workflow import extractable_text

from scripts.evaluate import evidence_matches


def test_chart_caption_alone_does_not_confirm_a_scan_was_recovered():
    raw = {
        "category": "figure",
        "content": {
            "html": "<figure><img><figcaption>부문별 점유율</figcaption></figure>",
            "text": "부문별 점유율",
        },
    }
    assert not extractable_text(raw, body_only=True)


def test_table_retrieval_evaluation_detects_wrong_value_on_correct_page():
    evidence = {
        "document_id": "doc",
        "page": 5,
        "row": 2,
        "column": 4,
        "contains_line": ["설비비", "2026년", "15.25"],
    }
    hit = {
        "document_id": "doc",
        "source_refs": [{"page": 5}],
        "table_segments": [
            {"row": 2, "column": 4, "text": "설비비 / 2026년 / 51.25"},
            {"row": 3, "column": 4, "text": "설비비 / 2026년 / 15.25"},
        ],
        "search_text": "설비비 / 2026년 / 51.25\n설비비 / 2026년 / 15.25",
    }
    assert not evidence_matches(evidence, hit)
    hit["table_segments"][0]["text"] = "설비비 / 2026년 / 115.25"
    assert not evidence_matches(evidence, hit)
    hit["table_segments"][0]["text"] = "설비비 / 2026년 / 15.25"
    assert evidence_matches(evidence, hit)
    hit["table_segments"][0]["column"] = 3
    assert not evidence_matches(evidence, hit)
