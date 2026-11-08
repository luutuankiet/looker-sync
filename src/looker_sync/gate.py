"""Syntax gate: refuse to push a LookML file that does not parse.

check(path, data) returns None when the file may be pushed, otherwise a one-line reason.

Two checks run on every `.lkml` file:
1. A brace scan that skips strings, `#` comments and `;;`-terminated blocks (sql, html,
   expression and the other keys the parser treats the same way). The vendored parser stops
   quietly at an extra closing brace and drops the rest of the file, so this scan is what
   catches it. It also names the line of an unterminated string or `;;` block, where the
   parser would only raise an index error.
2. The vendored `lkml` parser (lexer and parser only). Any exception it raises fails the gate.

`.dashboard.lookml` files are YAML and every other non-`.lkml` file passes unchecked.
"""
from __future__ import annotations

import logging
from typing import Optional

from ._vendor import lkml
import re

from ._vendor.lkml.tree import PairNode
from ._vendor.lkml.keys import EXPR_BLOCK_KEYS, PLURAL_KEYS, QUOTED_LITERAL_KEYS

# The parser logs through loggers under this name. Keep them away from the root logger and
# from logging's last-resort stderr handler, without touching the root logger itself.
_LKML_LOGGER = logging.getLogger(lkml.__name__)
_LKML_LOGGER.addHandler(logging.NullHandler())
_LKML_LOGGER.propagate = False
_LKML_LOGGER.setLevel(logging.CRITICAL + 1)

# Top-level keys that always open a block. A file cut short right after `view: name` still
# parses, as a plain key-value pair, so the gate refuses that shape.
_BLOCK_ONLY_KEYS = frozenset((
    'view', 'explore', 'datagroup', 'test', 'access_grant', 'named_value_format',
    'map_layer', 'aggregate_table',
))

_IDENT = frozenset('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_')
_BLOCK_KEYS = frozenset(EXPR_BLOCK_KEYS)
# LookML keys that never start a line of SQL or HTML. Seeing one at the start of a line
# inside a `;;` block means an earlier `;;` is missing: the block has swallowed the
# following parameters, which still parses because it ends at the next `;;`.
_LOOKML_KEYS = frozenset(EXPR_BLOCK_KEYS + PLURAL_KEYS + QUOTED_LITERAL_KEYS + (
    'type', 'hidden', 'primary_key', 'value_format', 'value_format_name', 'drill_fields',
    'fields', 'relationship', 'timeframes', 'datatype', 'convert_tz', 'allowed_value',
    'derived_table', 'persist_for', 'persist_with', 'datagroup_trigger', 'connection',
    'required_access_grants', 'always_filter', 'conditionally_filter', 'from',
    'view_name', 'tags', 'suggestions', 'can_filter', 'default_value', 'precision',
    'max_cache_age', 'url', 'icon_url', 'extension', 'required_fields',
))
_KEY_LINE = re.compile(r'^[ \t]*([A-Za-z_][A-Za-z0-9_]*):(?=[ \t]|$)', re.M)


def check(path: str, data: bytes) -> Optional[str]:
    """Return None if `data` (the bytes of `path`) may be pushed, else a one-line reason."""
    name = path.replace('\\', '/').rsplit('/', 1)[-1].lower()
    if name.endswith('.dashboard.lookml') or not name.endswith('.lkml'):
        return None
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError as e:
        return f'not valid UTF-8 at byte {e.start}'
    reason = _scan(text)
    if reason:
        return reason
    try:
        document = lkml.parse(text)
    except Exception as e:  # any parser failure holds the file
        msg = ' '.join(str(e).split()) or type(e).__name__
        return f'LookML does not parse: {msg}'
    for item in document.container.items:
        if isinstance(item, PairNode) and item.type.value in _BLOCK_ONLY_KEYS:
            return (f'{item.type.value}: {item.value.value} has no {{ }} block '
                    f'(file cut short?)')
    return None


def _scan(text: str) -> Optional[str]:
    """Check that `{` and `}` balance outside strings, comments and `;;` blocks, that every
    string and `;;` block ends, and that no `;;` block swallowed the parameters after it."""
    opened = []  # line number of each open brace
    line = 1
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == '\n':
            line += 1
        elif ch == '#':
            j = text.find('\n', i)
            i = n if j < 0 else j
            continue
        elif ch == '"':
            start = line
            i += 1
            while i < n and text[i] != '"':
                if text[i] == '\\':
                    i += 1
                if i < n and text[i] == '\n':
                    line += 1
                i += 1
            if i >= n:
                return f'unterminated string starting on line {start}'
        elif ch in _IDENT and (i == 0 or text[i - 1] not in _IDENT):
            j = i
            while j < n and text[j] in _IDENT:
                j += 1
            if j < n and text[j] == ':' and text[i:j] in _BLOCK_KEYS:
                end = text.find(';;', j + 1)
                if end < 0:
                    return f'{text[i:j]}: on line {line} is never closed with ;;'
                body = text[j + 1:end]
                for m in _KEY_LINE.finditer(body):
                    if m.start() > 0 and m.group(1).lower() in _LOOKML_KEYS:
                        return (f'{text[i:j]}: on line {line} is missing its ;; (it runs on '
                                f'into {m.group(1)}: on line '
                                f'{line + body.count(chr(10), 0, m.start(1))})')
                line += text.count('\n', i, end)
                i = end + 2
                continue
            i = j
            continue
        elif ch == '{':
            opened.append(line)
        elif ch == '}':
            if not opened:
                return f'extra closing brace on line {line}'
            opened.pop()
        i += 1
    if opened:
        return f'brace opened on line {opened[-1]} is never closed'
    return None
