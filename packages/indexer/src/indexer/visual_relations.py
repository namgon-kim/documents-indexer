"""Conservative links between original embedded images and document elements."""


def _same_scope(left, right):
    a, b = left.get('office_location') or {}, right.get('office_location') or {}
    if a.get('kind') == b.get('kind') == 'pptx_slide':
        return a.get('slide') == b.get('slide')
    if a.get('kind') == b.get('kind') == 'hwpx_section':
        return a.get('section') == b.get('section')
    if a.get('kind') == b.get('kind') == 'docx_block':
        return a.get('block') == b.get('block')
    if a.get('kind') == b.get('kind') == 'hwp_paragraph':
        return a.get('section') == b.get('section') and a.get('paragraph') == b.get('paragraph')
    return False


def _distance(left, right):
    a, b = left.get('layout') or {}, right.get('layout') or {}
    if a.get('space') != 'pptx_emu' or b.get('space') != 'pptx_emu':
        return None
    width, height = a.get('container_width'), a.get('container_height')
    if not width or not height or (width, height) != (b.get('container_width'), b.get('container_height')):
        return None
    dx = max(0, max(a['left'], b['left']) - min(a['left'] + a['width'], b['left'] + b['width'])) / width
    dy = max(0, max(a['top'], b['top']) - min(a['top'] + a['height'], b['top'] + b['height'])) / height
    return (dx * dx + dy * dy) ** 0.5


def apply_visual_relations(elements):
    images = [element for element in elements if element.get('source_origin') == 'native_image'
              and element.get('asset_id') and element.get('visual_id')]
    by_id = {image['visual_id']: image for image in images}
    for element in elements:
        if element.get('source_origin') == 'native_image':
            continue
        image = by_id.get(element.get('visual_id'))
        if image:
            is_description = element.get('source_origin') == 'visual_description'
            element['related_images'] = [{'element_id': image['element_id'], 'type': 'image_description' if is_description else 'image_analysis',
                                          'origin': 'user' if is_description else 'source', 'status': 'confirmed'}]
            continue
        if not element.get('text') or element.get('kind') == 'heading':
            continue
        same = [image for image in images if element.get('container_id') and
                element.get('container_id') == image.get('container_id') and _same_scope(element, image)]
        if len(same) == 1:
            element['related_images'] = [{'element_id': same[0]['element_id'], 'type': 'same_container',
                                          'origin': 'source', 'status': 'unverified'}]
            continue
        nearby = [(distance, image) for image in images if _same_scope(element, image)
                  and (distance := _distance(element, image)) is not None and distance <= 0.05]
        nearby.sort(key=lambda item: item[0])
        if nearby and (len(nearby) == 1 or nearby[1][0] - nearby[0][0] >= 0.02):
            element['related_images'] = [{'element_id': nearby[0][1]['element_id'], 'type': 'nearby',
                                          'origin': 'inferred', 'status': 'unverified'}]
        location = element.get('office_location') or {}
        if location.get('kind') == 'xlsx_range':
            candidates = [image for image in images if (image.get('layout') or {}).get('space') == 'xlsx_anchor'
                          and image['layout'].get('sheet') == location.get('sheet')]
            if candidates:
                element['related_images'] = [{'element_id': image['element_id'], 'type': 'cell_anchor',
                                              'origin': 'source', 'status': 'unverified'} for image in candidates]


def preserve_visual_relations(elements, previous):
    """Translate unchanged relationships to the new job's element IDs."""
    translated = {element['previous_element_id']: element for element in elements if element.get('previous_element_id')}
    for old in previous:
        current = translated.get(old.get('element_id'))
        if not current:
            continue
        relations = []
        for link in old.get('related_images', []):
            target = translated.get(link.get('element_id'))
            if target and target.get('source_origin') == 'native_image':
                relations.append({**link, 'element_id': target['element_id']})
        current['related_images'] = relations
