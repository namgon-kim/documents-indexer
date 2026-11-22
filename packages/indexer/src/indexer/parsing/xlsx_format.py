"""Render common Excel number formats while keeping typed cell values unchanged.

Only well-defined format codes are rendered. Conditional sections, scientific and
fraction formats, locale-specific calendars and other uncommon codes are reported
as unsupported so callers keep the canonical value instead of inventing a display.
"""

import re
from datetime import date, datetime, time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

MONTHS = ('January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September',
          'October', 'November', 'December')
DAYS = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')
# Built-in date formats whose rendering follows the workbook reader's locale. Use
# an unambiguous ISO rendering instead of the US mask that openpyxl reports.
LOCALE_DATE_FORMATS = {14: 'yyyy-mm-dd', 22: 'yyyy-mm-dd h:mm'}

_TOKEN = re.compile(r'"[^"]*"|\\.|\[[^\]]*\]|_.|\*.|AM/PM|am/pm|A/P|a/p|[yY]+|[mM]+|[dD]+|[hH]+|[sS]+'
                    r'|[eE][+-]|[0#?]|.', re.S)
_ELAPSED = re.compile(r'^\[(h+|m+|s+)\]$', re.IGNORECASE)
_CURRENCY = re.compile(r'^\[\$([^\]-]*)(?:-[0-9A-Fa-f]+)?\]$')
_COLOR = re.compile(r'^\[(?:black|blue|cyan|green|magenta|red|white|yellow|color\s*\d{1,2})\]$', re.IGNORECASE)
_LOCALE = re.compile(r'^\[\$-[0-9A-Fa-f]+\]$')


class Unsupported(Exception):
    """The format code cannot be rendered faithfully."""


def _sections(code):
    sections, current, quoted, bracket = [], [], False, False
    escaped = False
    for char in code:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == '\\' and not quoted:
            current.append(char)
            escaped = True
            continue
        if char == '"' and not bracket:
            quoted = not quoted
        elif char == '[' and not quoted:
            bracket = True
        elif char == ']' and not quoted:
            bracket = False
        if char == ';' and not quoted and not bracket:
            sections.append(''.join(current))
            current = []
            continue
        current.append(char)
    sections.append(''.join(current))
    return sections


def _literal(token):
    """Return literal output for a non-placeholder token, or None for a code token."""
    if token.startswith('"'):
        return token[1:-1]
    if token.startswith('\\'):
        return token[1:]
    if token.startswith('_'):
        return ' '
    if token.startswith('*'):
        return ''
    if token.startswith('['):
        if _COLOR.match(token) or _LOCALE.match(token):
            return ''
        currency = _CURRENCY.match(token)
        if currency:
            return currency.group(1)
        if _ELAPSED.match(token):
            return None
        raise Unsupported(token)
    return None


def _is_date_token(token):
    lower = token.lower()
    return (lower[0] in 'ymdhs' and lower.isalpha() and set(lower) == {lower[0]}) or lower in {'am/pm', 'a/p'} or bool(
        _ELAPSED.match(token))


def classify(code):
    """Classify a format code as general, percent, datetime, duration, number, text or unsupported."""
    if not code or code.strip().lower() == 'general':
        return 'general'
    try:
        tokens = _TOKEN.findall(_sections(code)[0])
        codes = [token for token in tokens if _literal(token) is None]
    except Unsupported:
        return 'unsupported'
    if any(_ELAPSED.match(token) for token in codes):
        return 'duration'
    if any(_is_date_token(token) for token in codes):
        return 'datetime'
    if '%' in codes:
        return 'percent'
    if any(token in {'0', '#', '?'} for token in codes):
        return 'number'
    if '@' in codes:
        return 'text'
    return 'unsupported'


def general_number(value):
    """Excel's General format keeps at most 15 significant digits."""
    if isinstance(value, bool):
        return 'TRUE' if value else 'FALSE'
    if isinstance(value, int):
        return str(value)
    text = format(value, '.15g')
    if 'e' in text:
        mantissa, exponent = text.split('e')
        text = f'{mantissa}E{exponent[0]}{exponent[1:].zfill(2)}'
    return text


def _round(number, places):
    return number.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def _number(value, tokens, signed):
    if any(token[:1] in {'e', 'E'} and len(token) == 2 for token in tokens) or '/' in tokens:
        raise Unsupported('exponent or fraction')
    number = Decimal(str(value))
    number *= Decimal(100) ** tokens.count('%')
    placeholders = [index for index, token in enumerate(tokens) if token in {'0', '#', '?'}]
    if not placeholders:
        raise Unsupported('no digit placeholder')
    first, last = placeholders[0], placeholders[-1]
    point = next((index for index in range(first, last + 1) if tokens[index] == '.'), None)
    # Commas directly after the final integer placeholder scale by thousands.
    scale = 0
    if point is None:
        integer_end = last + 1
        trailing = integer_end
        while trailing < len(tokens) and tokens[trailing] == ',':
            scale += 1
            trailing += 1
    else:
        integer_end = point
        while integer_end - 1 > first and tokens[integer_end - 1] == ',':
            scale += 1
            integer_end -= 1
    number /= Decimal(1000) ** scale
    integer_tokens = tokens[first:integer_end]
    fraction_tokens = [token for token in tokens[point + 1:last + 1]] if point is not None else []
    fraction_slots = [token for token in fraction_tokens if token in {'0', '#', '?'}]
    rounded = _round(abs(number), len(fraction_slots))
    negative = number < 0 and rounded != 0
    digits = format(rounded, 'f')
    whole, _, fraction = digits.partition('.')
    grouping = ',' in integer_tokens
    slots = [token for token in integer_tokens if token in {'0', '#', '?'}]
    interior = [token for token in integer_tokens if token not in {'0', '#', '?', ','}]
    if whole == '0' and slots and all(token != '0' for token in slots):
        whole = ''
    if interior and not grouping:
        output = []
        remaining = list(whole)
        for token in reversed(integer_tokens):
            if token in {'0', '#', '?'}:
                if remaining:
                    output.append(remaining.pop())
                else:
                    output.append({'0': '0', '#': '', '?': ' '}[token])
            else:
                literal = _literal(token)
                output.append(token if literal is None else literal)
        integer_text = ''.join(remaining) + ''.join(reversed(output))
    else:
        minimum = slots.count('0')
        padded = whole.rjust(minimum, '0')
        if grouping and padded:
            padded = f'{int(padded):,}'.rjust(minimum, '0') if padded.isdigit() else padded
        spaces = slots.count('?') - max(0, len(padded) - minimum) if padded else slots.count('?')
        integer_text = ' ' * max(0, spaces) + padded
    fraction_text = ''
    if point is not None:
        chars = list(fraction.ljust(len(fraction_slots), '0'))
        for index in range(len(fraction_slots) - 1, -1, -1):
            if fraction_slots[index] == '0' or chars[index] != '0':
                break
            chars[index] = ' ' if fraction_slots[index] == '?' else ''
        fraction_text = '.' + ''.join(chars)
    prefix = ''.join(_render_literal(token) for token in tokens[:first])
    suffix = ''.join(_render_literal(token) for token in tokens[last + 1:] if token != ',')
    sign = '-' if negative and signed else ''
    return sign + prefix + integer_text + fraction_text + suffix


def _render_literal(token):
    literal = _literal(token)
    if literal is not None:
        return literal
    if token in {'%', '.', ','} or len(token) == 1:
        return token
    raise Unsupported(token)


def _datetime(moment, tokens, elapsed_seconds=None):
    """Render y/m/d/h/s codes. `elapsed_seconds` supplies [h], [m] and [s] totals."""
    codes = [(index, token) for index, token in enumerate(tokens) if _literal(token) is None and _is_date_token(token)]
    twelve = any(token.lower() in {'am/pm', 'a/p'} for _, token in codes)
    minute_positions = set()
    for position, (index, token) in enumerate(codes):
        if token[0] not in 'mM' or len(token) > 2:
            continue
        previous = codes[position - 1][1].lower() if position else ''
        following = codes[position + 1][1].lower() if position + 1 < len(codes) else ''
        if previous.startswith(('h', '[h')) or following.startswith(('s', '[s')):
            minute_positions.add(index)
    fraction_digits = 0
    for index, token in enumerate(tokens):
        if token == '.' and index > 0 and any(t.lower().startswith(('s', '[s')) for t in tokens[:index] if _literal(t) is None):
            run = 0
            while index + 1 + run < len(tokens) and tokens[index + 1 + run] == '0':
                run += 1
            fraction_digits = max(fraction_digits, run)
    seconds_value = None
    if elapsed_seconds is not None:
        total = Decimal(str(elapsed_seconds))
        seconds_value = _round(total, fraction_digits)
    output = []
    skip = set()
    for index, token in enumerate(tokens):
        if index in skip:
            continue
        literal = _literal(token)
        if literal is not None:
            output.append(literal)
            continue
        lower = token.lower()
        elapsed = _ELAPSED.match(token)
        if elapsed:
            if seconds_value is None:
                raise Unsupported('elapsed time without duration')
            unit = elapsed.group(1).lower()[0]
            width = len(elapsed.group(1))
            whole = int(seconds_value)
            amount = whole // 3600 if unit == 'h' else whole // 60 if unit == 'm' else whole
            output.append(str(amount).zfill(width))
            continue
        if lower in {'am/pm', 'a/p'}:
            if moment is None:
                raise Unsupported('meridiem without clock time')
            text = 'AM' if moment.hour < 12 else 'PM'
            output.append(text if lower == 'am/pm' else text[0])
            continue
        if token == '.' and fraction_digits and index + 1 < len(tokens) and tokens[index + 1] == '0':
            run = 0
            while index + 1 + run < len(tokens) and tokens[index + 1 + run] == '0':
                skip.add(index + 1 + run)
                run += 1
            if seconds_value is not None:
                fraction = seconds_value - int(seconds_value)
            else:
                fraction = _round(Decimal(moment.microsecond) / Decimal(1000000), run)
            output.append('.' + format(fraction, 'f').partition('.')[2].ljust(run, '0')[:run])
            continue
        if not _is_date_token(token):
            output.append(token)
            continue
        code = lower[0]
        width = len(lower)
        if code in 'hms' and seconds_value is not None and moment is None:
            whole = int(seconds_value)
            hours, remainder = divmod(whole, 3600)
            minutes, seconds = divmod(remainder, 60)
            amount = {'h': hours % 24, 'm': minutes, 's': seconds}[code]
            if code == 'm' and index not in minute_positions:
                raise Unsupported('month without date')
            output.append(str(amount).zfill(min(width, 2)))
            continue
        if moment is None:
            raise Unsupported('date code without date')
        if code == 'y':
            output.append(f'{moment.year % 100:02}' if width <= 2 else f'{moment.year:04}')
        elif code == 'm' and index in minute_positions:
            output.append(str(moment.minute).zfill(min(width, 2)))
        elif code == 'm':
            month = moment.month
            output.append(str(month) if width == 1 else f'{month:02}' if width == 2 else MONTHS[month - 1][:3]
                          if width == 3 else MONTHS[month - 1] if width == 4 else MONTHS[month - 1][0])
        elif code == 'd':
            output.append(str(moment.day) if width == 1 else f'{moment.day:02}' if width == 2
                          else DAYS[moment.weekday()][:3] if width == 3 else DAYS[moment.weekday()])
        elif code == 'h':
            hour = moment.hour % 12 or 12 if twelve else moment.hour
            output.append(str(hour).zfill(min(width, 2)))
        elif code == 's':
            output.append(str(moment.second).zfill(min(width, 2)))
        else:
            raise Unsupported(token)
    return ''.join(output)


def _moment(value, kind):
    if kind == 'datetime':
        return datetime.fromisoformat(value)
    if kind == 'date':
        day = date.fromisoformat(value)
        return datetime(day.year, day.month, day.day)
    if kind == 'time':
        clock = time.fromisoformat(value)
        return datetime(1900, 1, 1, clock.hour, clock.minute, clock.second, clock.microsecond)
    return None


def render(value, kind, number_format, number_format_id=None):
    """Return (text, display_kind, applied_format).

    display_kind is 'formatted' for a rendered mask, 'canonical' for the General
    format and 'unsupported_format' when the canonical value is kept because the
    mask is outside the supported subset.
    """
    if kind == 'boolean':
        return ('TRUE' if value else 'FALSE'), 'formatted', number_format
    if value is None or kind in {'null', 'string', 'error'}:
        return ('' if value is None else str(value)), 'canonical', None
    code = LOCALE_DATE_FORMATS.get(number_format_id, number_format) if kind in {'date', 'datetime'} else number_format
    category = classify(code)
    canonical = general_number(value) if kind in {'integer', 'number'} else str(value)
    if category == 'general':
        return canonical, 'canonical', None
    if category in {'unsupported', 'text'}:
        return canonical, ('unsupported_format' if category == 'unsupported' else 'canonical'), None
    sections = _sections(code)
    try:
        if kind in {'integer', 'number'}:
            if category not in {'number', 'percent'}:
                return canonical, 'unsupported_format', None
            number = Decimal(str(value))
            section, signed = sections[0], True
            if len(sections) >= 3 and number == 0:
                section, signed = sections[2], False
            elif len(sections) >= 2 and number < 0:
                section, signed = sections[1], False
            if section.startswith('[') and re.match(r'^\[[<>=]', section):
                raise Unsupported('conditional section')
            return _number(value, _TOKEN.findall(section), signed), 'formatted', code
        if kind == 'duration':
            if category != 'duration' and category != 'datetime':
                return canonical, 'unsupported_format', None
            tokens = _TOKEN.findall(sections[0])
            return _datetime(None, tokens, elapsed_seconds=value), 'formatted', code
        if kind in {'date', 'datetime', 'time'} and category == 'datetime':
            return _datetime(_moment(value, kind), _TOKEN.findall(sections[0])), 'formatted', code
    except (Unsupported, InvalidOperation, ValueError, IndexError, OverflowError):
        return canonical, 'unsupported_format', None
    return canonical, 'unsupported_format', None


def value_category(number_format, value_type):
    """A small, display-independent hint for planning typed filters."""
    if value_type in {'date', 'datetime', 'time'}:
        return value_type
    if value_type == 'duration':
        return 'duration_seconds'
    if value_type in {'integer', 'number'}:
        return {'percent': 'percent_fraction'}.get(classify(number_format), 'number')
    return value_type
