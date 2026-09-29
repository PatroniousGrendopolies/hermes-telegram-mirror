"""Safe Markdown subset rendered as styled text spans before balanced chunking."""
import re
from html import escape


def _inline(text, tags=(), depth=0):
    if depth > 8:
        return [(text, tags)]
    pattern = re.compile(r'`([^`]+)`|\[([^\]]+)\]\(([^\s)]+)\)|\*\*\*(.+?)\*\*\*|\*\*(.+?)\*\*|__(.+?)__|\*([^*]+)\*|(?<!\w)_([^_]+)_(?!\w)')
    spans, pos = [], 0
    for match in pattern.finditer(text):
        spans.append((text[pos:match.start()], tags))
        code, label, url, both, bold, bold2, italic, italic2 = match.groups()
        if code is not None:
            spans.append((code, tags + (('code', '<code>'),)))
        elif label is not None:
            link = (('a', f'<a href="{escape(url, quote=True)}">'),) if url.lower().startswith(('http://', 'https://')) and len(url) < 1000 else ()
            spans.extend(_inline(label, tags + link, depth + 1))
        else:
            extra = (('b', '<b>'), ('i', '<i>')) if both else ((('b', '<b>'),) if bold or bold2 else (('i', '<i>'),))
            spans.extend(_inline(both or bold or bold2 or italic or italic2, tags + extra, depth + 1))
        pos = match.end()
    spans.append((text[pos:], tags))
    return spans


def _cells(line):
    return [x.strip() for x in line.strip().strip('|').split('|')]


def _separator(line):
    cells = _cells(line)
    return '|' in line and all(re.fullmatch(r':?-{3,}:?', c.replace(' ', '')) for c in cells)


def spans(markdown):
    lines = markdown.split('\n')
    result, i = [], 0
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith('```'):
            i += 1
            code = []
            while i < len(lines) and not lines[i].lstrip().startswith('```'):
                code.append(lines[i])
                i += 1
            result.append(('\n'.join(code), (('pre', '<pre>'),)))
        elif i + 1 < len(lines) and _separator(lines[i + 1]):
            headers = _cells(line)
            i += 2
            while i < len(lines) and '|' in lines[i] and lines[i].strip():
                result.append(('• ', ()))
                cells = _cells(lines[i])
                for j, title in enumerate(headers):
                    if j:
                        result.append((' · ', ()))
                    result.extend(_inline(title + ': ' + (cells[j] if j < len(cells) else '')))
                result.append(('\n', ()))
                i += 1
            continue
        else:
            heading = re.match(r'^\s*#{1,6}\s+(.*)', line)
            bullet = re.match(r'^(\s*)[-*+]\s+(.*)', line)
            if heading:
                result.extend(_inline(heading[1], (('b', '<b>'),)))
            elif bullet:
                result.append((bullet[1] + '• ', ()))
                result.extend(_inline(bullet[2]))
            else:
                result.extend(_inline(line))
        if i < len(lines) - 1:
            result.append(('\n', ()))
        i += 1
    return result


def _units(text):
    return len(text.encode('utf-16-le')) // 2


def render_chunks(markdown, limit=3800):
    if limit < 64:
        raise ValueError('HTML chunk limit must be at least 64')
    result, body, plain, active = [], '', '', ()

    def closing(tags):
        return ''.join(f'</{name}>' for name, _ in reversed(tags))

    def flush():
        nonlocal body, plain, active
        if plain:
            result.append({'html': body + closing(active), 'plain': plain})
        body, plain, active = '', '', ()

    for text, tags in spans(markdown):
        # An unusually long link attribute must not make even one char unchunkable.
        if _units(''.join(t[1] for t in tags) + closing(tags)) > limit - 16:
            tags = ()
        for word in re.findall(r'\s+|\S+', text):
            switch = '' if tags == active else closing(active) + ''.join(t[1] for t in tags)
            encoded = escape(word, quote=False)
            if plain and _units(body + switch + encoded + closing(tags)) > limit:
                flush()
            for char in word:
                switch = '' if tags == active else closing(active) + ''.join(t[1] for t in tags)
                encoded = escape(char, quote=False)
                if _units(body + switch + encoded + closing(tags)) > limit:
                    flush()
                    switch = ''.join(t[1] for t in tags)
                body += switch + encoded
                plain += char
                active = tags
    flush()
    return result or [{'html': '', 'plain': ''}]
