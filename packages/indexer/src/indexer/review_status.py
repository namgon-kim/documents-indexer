"""Separate known extraction limitations from actionable incomplete results."""

HWP_NESTED_TABLE_NOTICE = 'HWP 중첩 표는 별도 표로 추출했습니다.'
HWP_NESTED_TABLE_LIMITATION = 'HWP 중첩 표는 별도 표로 추출되어 원래의 상위 셀 관계가 보존되지 않습니다.'


def normalize_review_status(status):
    """Interpret existing native HWP jobs without changing their stored artifacts."""
    coverage = status.get('coverage') or {}
    warnings = coverage.get('warnings') or []
    if status.get('provider') != 'native_hwp' or HWP_NESTED_TABLE_NOTICE not in warnings:
        return status
    remaining = [warning for warning in warnings if warning != HWP_NESTED_TABLE_NOTICE]
    limitations = list(dict.fromkeys([*(coverage.get('limitations') or []), HWP_NESTED_TABLE_LIMITATION]))
    coverage = {**coverage, 'warnings': remaining, 'limitations': limitations}
    if (not remaining and not status.get('provider_unavailable') and not status.get('provider_failed')
        and not coverage.get('missing_pages') and not coverage.get('unavailable_pages')):
        coverage['provider_incomplete'] = False
    return {**status, 'coverage': coverage}
