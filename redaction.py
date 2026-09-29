"""Worker-side conservative argument previews. Never accepts tool output bodies."""
import re

from .transport import scrub


SECRET_NAME = r'(?:api[_-]?key|private[_-]?key|key|access[_-]?token|refresh[_-]?token|token|password|passwd|secret|credential|authorization|cookie|cvc)'


def redact_args(text: str, token: str = '') -> str:
    text = scrub(text, token)
    # PEMs, authorization headers, quoted JSON/dict secret fields.
    text = re.sub(r'-----BEGIN [^-]+-----[\s\S]*?(?:-----END [^-]+-----|$)', '[REDACTED]', text)
    text = re.sub(r'(?i)\b(?:Bearer|Basic)\s+[^\s\"\'<>]+', '[REDACTED]', text)
    text = re.sub(rf'''(?ix)(["']?{SECRET_NAME}["']?\s*[:=]\s*)(?:"[^"\n]*(?:"|$)|'[^'\n]*(?:'|$)|[^\s,;}}]+)''', r'\1[REDACTED]', text)
    # ALL shell environment assignments, not just well-known credential names.
    text = re.sub(r'''\b([A-Za-z_][A-Za-z_0-9]*=)(?:"[^"\n]*(?:"|$)|'[^'\n]*(?:'|$)|[^\s;]+)''', r'\1[REDACTED]', text)
    text = re.sub(rf'''(?i)(--?{SECRET_NAME}(?:\s+|=))(?:"[^"\n]*(?:"|$)|'[^'\n]*(?:'|$)|[^\s]+)''', r'\1[REDACTED]', text)
    text = re.sub(r'https?://[^\s/@:]+:[^\s/@]+@', 'https://[REDACTED]@', text)
    text = re.sub(r'\b(?:sk-|gh[pousr]_|github_pat_|xox[baprs]-)[A-Za-z0-9_-]+', '[REDACTED]', text)
    # Catch unlabeled opaque credentials, including truncated long values.
    text = re.sub(r'[A-Za-z0-9_+/=-]{28,}', '[REDACTED]', text)
    return text


def preview(name: str, primary: str, token: str = '') -> str:
    clean = redact_args(primary, token).replace('\n', ' ').replace('\r', ' ')
    return f"🔧 {re.sub(r'[^a-zA-Z0-9_:.-]', '', name)[:60]}: {clean[:120]}"
