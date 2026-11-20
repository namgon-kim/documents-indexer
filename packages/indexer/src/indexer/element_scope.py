"""Conservative source scopes for page-less native document elements."""


def source_scope(element):
    page = element.get('page')
    if page is not None:
        return f'page:{page}'
    if element.get('source_origin') in {'visual_analysis', 'visual_description'} and element.get('visual_id'):
        return f"visual:{element['visual_id']}"
    location = element.get('office_location') or {}
    kind = location.get('kind')
    if kind == 'pptx_slide' and location.get('slide') is not None:
        return f"slide:{location['slide']}"
    if kind == 'hwpx_section' and location.get('section'):
        return f"section:{location['section']}"
    if kind == 'hwp_paragraph' and type(location.get('section')) is int:
        return f"hwp-section:{location['section']}"
    if kind in {'xlsx_cell', 'xlsx_range'} and location.get('sheet'):
        return f"sheet:{location['sheet']}"
    if kind == 'docx_block':
        return 'native:docx'
    if element.get('source_origin') == 'native_extraction':
        return 'native:document'
    return None


def same_scope(left, right):
    scope = source_scope(left)
    return scope is not None and scope == source_scope(right)
