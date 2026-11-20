"""Apply explicit image choices to an immutable prior parsing result."""

import hashlib
import re
import tempfile
from pathlib import Path

from indexer.contracts import IndexerError
from indexer.parsing.contracts import ParseRequest, ParseResult
from indexer.parsing.native_documents import image_for_analysis

IMAGE_ACTIONS = {'analyze', 'original', 'both', 'exclude', 'reuse', 'describe'}


def apply_image_actions(base, actions, parser, image_path, checkpoint, persist, progress, descriptions=None):
    elements = base['elements']
    anchors = {item.get('_visual_id'): item for item in elements if item.get('_origin') == 'native_image'}
    if not actions or any(key not in anchors or value not in IMAGE_ACTIONS for key, value in actions.items()):
        raise IndexerError('IMAGE_ACTION_INVALID', '이미지 선택 또는 처리 방식을 확인해 주세요.', 422)
    selected = {key for key, value in actions.items() if value in {'analyze', 'both'}}
    descriptions = descriptions or {}
    for visual_id, action in actions.items():
        if action == 'describe' and (not isinstance(descriptions.get(visual_id), str)
                                     or not 1 <= len(descriptions[visual_id].strip()) <= 2000):
            raise IndexerError('IMAGE_DESCRIPTION_REQUIRED', '이미지 설명을 1~2,000자로 입력해 주세요.', 422)
    available_analysis = {item.get('_visual_id') for item in elements if item.get('_origin') == 'visual_analysis'}
    if any(value == 'reuse' and key not in available_analysis for key, value in actions.items()):
        raise IndexerError('IMAGE_ANALYSIS_NOT_FOUND', '재사용할 이미지 분석 결과가 없습니다.', 409)
    if selected and getattr(getattr(parser, 'config', None), 'provider', '') != 'paddle_vl':
        raise IndexerError('VISUAL_PARSER_UNSUPPORTED', '이미지 분석은 현재 PaddleOCR-VL에서 지원합니다.', 422)
    completed = {item['index']: item for item in (checkpoint or {}).get('visuals', []) if type(item.get('index')) is int}
    replacements = {}
    if selected and progress:
        progress(0, len(selected))
    for count, visual_id in enumerate(sorted(selected), 1):
        anchor = anchors[visual_id]
        match = re.fullmatch(r'image-(\d+)', visual_id)
        if not match or int(match.group(1)) >= 100:
            raise IndexerError('IMAGE_ACTION_INVALID', '이미지 식별자를 확인할 수 없습니다.', 422)
        index = int(match.group(1))
        asset_id = anchor.get('_asset_id', '')
        data = image_path(asset_id).read_bytes()
        if hashlib.sha256(data).hexdigest() != asset_id:
            raise IndexerError('IMAGE_ASSET_INVALID', '원본 이미지의 무결성을 확인할 수 없습니다.', 409)
        saved = completed.get(index)
        if saved and (saved.get('sha256') != asset_id or saved.get('status') != 'completed'):
            raise IndexerError('VISUAL_OUTCOME_UNKNOWN', '이미지 요청의 완료 여부를 확인할 수 없습니다.', 409)
        if saved:
            raw_elements = saved.get('elements', [])
        else:
            prepared = image_for_analysis(data, anchor.get('_image_format') or '')
            if prepared is None:
                raise IndexerError('VISUAL_FORMAT_UNSUPPORTED', '선택한 이미지 형식 또는 크기는 분석할 수 없습니다.', 422)
            payload, suffix = prepared
            if persist:
                persist('visual', {'index': index, 'sha256': asset_id, 'status': 'started'})
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / ('image.' + suffix)
                path.write_bytes(payload)
                result = parser.parse(ParseRequest(path, suffix, page_count=1,
                                                   timeout_seconds=getattr(parser, 'image_timeout_seconds', None)))
            if result.incomplete or result.unavailable:
                raise IndexerError('VISUAL_RESULT_INCOMPLETE', '선택한 이미지 분석 결과를 확인할 수 없습니다.', 502)
            raw_elements = result.elements
            if persist:
                persist('visual', {'index': index, 'sha256': asset_id, 'status': 'completed', 'elements': raw_elements})
        output = []
        for item in raw_elements:
            content = item.get('content') or {}
            if not (content.get('text') or content.get('html')):
                continue
            category = item.get('category', 'figure')
            if category == 'heading':
                category, content = 'paragraph', {**content, 'html': ''}
            output.append({'id': f'visual-{index}', 'page': None, 'category': category, 'content': content,
                           'source_location': anchor.get('source_location'), '_origin': 'visual_analysis',
                           '_asset_id': asset_id, '_visual_id': visual_id,
                           '_layout': anchor.get('_layout'), '_container': anchor.get('_container')})
        replacements[visual_id] = output
        if progress:
            progress(count, len(selected))
    merged = []
    for position, item in enumerate(elements, 1):
        item = {**item, '_merge_from': f'e{position}'}
        visual_id = item.get('_visual_id')
        if item.get('_origin') == 'visual_analysis' and visual_id in actions:
            action = actions[visual_id]
            if action in {'analyze', 'both'}:
                continue
            item = {**item, '_image_suppressed': action in {'original', 'exclude', 'describe'}}
        if item.get('_origin') == 'visual_description' and visual_id in actions:
            item = {**item, '_image_suppressed': True}
        if item.get('_origin') == 'native_image' and visual_id in actions:
            item = {**item, '_image_policy': 'both' if actions[visual_id] == 'reuse' else actions[visual_id]}
            merged.append(item)
            merged.extend(replacements.get(visual_id, []))
            if actions[visual_id] == 'describe':
                merged.append({'id': f'description-{visual_id}', 'page': None, 'category': 'paragraph',
                               'content': {'text': descriptions[visual_id].strip(), 'html': ''},
                               'source_location': item.get('source_location'), '_origin': 'visual_description',
                               '_asset_id': item.get('_asset_id'), '_visual_id': visual_id,
                               '_layout': item.get('_layout'), '_container': item.get('_container')})
        else:
            merged.append(item)
    raw = {**base['raw'], 'image_actions': actions}
    resolved = {key for key, value in replacements.items() if value}
    resolved.update(item.get('_visual_id') for item in merged if item.get('_origin') == 'visual_analysis'
                    and not item.get('_image_suppressed') and any((item.get('content') or {}).get(field) for field in ('text', 'html')))
    resolved.update(item.get('_visual_id') for item in merged if item.get('_origin') == 'native_image'
                    and item.get('_image_policy') in {'original', 'exclude', 'describe'})
    warnings = list(raw.get('warnings', []))
    # Older draft jobs stored format limitations as unresolved warnings.
    format_limitations = {
        'DOCX 직접 추출은 페이지 배치, 머리글·바닥글과 도형 연결을 보장하지 않습니다.',
        'PPTX 직접 추출은 도형의 배치·연결 관계와 발표자 노트를 보장하지 않습니다.',
        'HWP 직접 추출은 본문 텍스트만 보존하며 표·배치 관계와 이미지 위치는 확인하지 못합니다.',
        'HWPX 직접 추출은 쪽 배치, 일부 스타일과 도형 관계를 확인하지 못합니다.',
    }
    from indexer.review_status import HWP_NESTED_TABLE_LIMITATION, HWP_NESTED_TABLE_NOTICE
    if HWP_NESTED_TABLE_NOTICE in warnings:
        raw['limitations'] = list(dict.fromkeys([*raw.get('limitations', []), HWP_NESTED_TABLE_LIMITATION]))
        warnings = [warning for warning in warnings if warning != HWP_NESTED_TABLE_NOTICE]
    raw['limitations'] = list(dict.fromkeys([*raw.get('limitations', []), *(w for w in warnings if w in format_limitations)]))
    warnings = [warning for warning in warnings if warning not in format_limitations]
    if set(anchors) <= resolved:
        hwp_unlinked = '본문과의 위치를 확인하지 못한 HWP 이미지가 있습니다.'
        if hwp_unlinked in warnings:
            raw['limitations'] = list(dict.fromkeys([*raw.get('limitations', []), hwp_unlinked]))
            warnings = [warning for warning in warnings if warning != hwp_unlinked]
        warnings = [warning for warning in warnings if not (
            warning.startswith('이미지 ') and warning.endswith('개는 분석하지 않았습니다.') or
            warning in {'지원하지 않거나 너무 큰 이미지가 있어 분석하지 못했습니다.',
                        '지원하지 않는 형식이거나 여러 프레임·너무 큰 이미지가 있어 분석하지 못했습니다.',
                        '텍스트를 확인할 수 없는 이미지가 있습니다.',
                        '결과를 확인할 수 없는 이미지가 있습니다.',
                        '이미지 분석 응답을 확인하지 못해 추가 분석을 중단했습니다. 이미지 목록에서 처리 방식을 선택해 주세요.',
                        '이전 이미지 요청의 완료 여부를 확인할 수 없어 추가 분석을 중단했습니다. 이미지 목록에서 처리 방식을 선택해 주세요.'})]
    if any(not replacement for replacement in replacements.values()) and '텍스트를 확인할 수 없는 이미지가 있습니다.' not in warnings:
        warnings.append('텍스트를 확인할 수 없는 이미지가 있습니다.')
    raw['warnings'] = warnings
    return ParseResult(base['provider'], merged, raw, incomplete=bool(warnings) or bool(base.get('unavailable'))
                       or bool((base.get('coverage') or {}).get('unavailable_pages')))
