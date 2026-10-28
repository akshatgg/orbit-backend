"""Text clean-up for imported events: strip HTML, tidy whitespace, enforce length limits.

A line-for-line port of Orbit's `src/lib/sources/text.ts`. Synced descriptions and
summaries are compared as strings on every run, so every regex below reproduces the
JavaScript one exactly (JavaScript's `\\s`, `^`/`$` and UTF-16 lengths included).
"""

import re

from . import js, urls

LIMITS = {
    "title": 140,  # events.title check constraint
    "summary": 200,  # events.summary check constraint
    "description": 5000,
    "venue": 200,
    "address": 300,
    "url": 2000,
}

_NAMED_ENTITIES = {
    "amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'", "nbsp": " ", "ndash": "–", "mdash": "—",
    "hellip": "…", "rsquo": "’", "lsquo": "‘", "rdquo": "”", "ldquo": "“", "pound": "£", "euro": "€", "copy": "©",
}

# JavaScript's multiline ^ and $ also break at \r,   and  .
_BOL = r"(?:^|(?<=[\r  ]))"
_EOL = r"(?=[\n\r  ]|\Z)"

_ENTITY = re.compile(r"&(#x[0-9a-f]+|#[0-9]+|[a-z]+);", re.I | re.A)


def _entity(match: re.Match) -> str:
    body = match.group(1)
    if body[0] == "#":
        try:
            code = int(body[2:], 16) if body[1] in "xX" else int(body[1:], 10)
        except ValueError:
            return match.group(0)
        if 0 < code <= 0x10FFFF:
            if 0xD800 <= code <= 0xDFFF:
                return match.group(0)  # a lone surrogate can't be stored
            return chr(code)
        return match.group(0)
    return _NAMED_ENTITIES.get(body.lower(), match.group(0))


def decode_entities(s: str) -> str:
    return _ENTITY.sub(_entity, s)


_CRLF = re.compile(r"\r\n?")
_SCRIPT = re.compile(r"<(script|style)[^>]*>[\s\S]*?</\1>", re.I | re.A)
_COMMENT = re.compile(r"<!--[\s\S]*?-->")
_BR = re.compile(f"<br{js.S}*/?>", re.I | re.A)
_BLOCK_END = re.compile(r"</(p|div|li|h[1-6]|tr)>", re.I | re.A)
_LI = re.compile(r"<li[^>]*>", re.I | re.A)
_TAG = re.compile(r"<[^>]+>")
_INLINE_SPACE = re.compile(r"[\t\f\v ]+")
_SPACE_NEWLINE = re.compile(r" *\n *")
_BLANKS = re.compile(r"\n{3,}")


def strip_html(value: str | None) -> str:
    """Turns HTML (or plain text) into tidy plain text, keeping paragraph breaks."""
    if not value:
        return ""
    text = _CRLF.sub("\n", value)
    text = _SCRIPT.sub("", text)
    text = _COMMENT.sub("", text)
    text = _BR.sub("\n", text)
    text = _BLOCK_END.sub("\n", text)
    text = _LI.sub("• ", text)
    text = _TAG.sub("", text)
    text = decode_entities(text)
    text = _INLINE_SPACE.sub(" ", text)
    text = _SPACE_NEWLINE.sub("\n", text)
    text = _BLANKS.sub("\n\n", text)
    return js.trim(text)


_SEPARATOR_LINE = re.compile(f"{_BOL}[ \\t]*[\\\\|*_=~+\\-–—]{{3,}}[ \\t]*{_EOL}", re.M)
_ORDERED_ITEM = re.compile(r"^[ \t]*[0-9]+[.)][ \t]+(.*)$", re.S)
_ORDERED_START = re.compile(r"^[ \t]*[0-9]+[.)][ \t]+")
_INDENTED = re.compile(f"^[ \\t]+{js.NS}")


def _line_item(line: str) -> re.Match | None:
    # JS `.` stops at \r,   and   (lines were split on \n only).
    match = _ORDERED_ITEM.match(line)
    if match and re.search(r"[\r  ]", match.group(1)):
        return None
    return match


def _renumber_ordered_lists(value: str) -> str:
    lines = value.split("\n")
    number = 0
    for i, line in enumerate(lines):
        item = _line_item(line)
        if item:
            number += 1
            lines[i] = f"{number}. {item.group(1)}"
            continue
        if js.trim(line) == "":
            nxt = i + 1
            while nxt < len(lines) and js.trim(lines[nxt]) == "":
                nxt += 1
            if nxt >= len(lines) or not _ORDERED_START.match(lines[nxt]):
                number = 0
            continue
        # An indented line is normally a wrapped continuation of the current list item.
        if number > 0 and _INDENTED.match(line):
            continue
        number = 0
    return "\n".join(lines)


_ZERO_WIDTH = re.compile(r"[​-‍﻿]")
_ESCAPED = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|>~,])")
_IMAGE = re.compile(r"!\[([^\]]*)\]\((https?://[^)" + js.S_IN + r"]+)(?:" + js.S + r"+\"[^\"]*\")?\)")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)" + js.S_IN + r"]+)(?:" + js.S + r"+\"[^\"]*\")?\)")
_BULLET = re.compile(f"{_BOL}[ \\t]*[*+-][ \\t]+", re.M)
_KEEP_BOLD = re.compile(f"(__)(?={js.NS})([\\s\\S]*?{js.NS})\\1")
_KEEP_ITALIC = re.compile(
    f"({_BOL}|[{js.S_IN}(])_(?={js.NS})([^_\\n]*?{js.NS})_(?={_EOL}|[{js.S_IN}).,!?:;])", re.M
)
_STRIP_BOLD = re.compile(f"(\\*\\*|__)(?={js.NS})([\\s\\S]*?{js.NS})\\1")
_STRIP_STAR = re.compile(f"\\*(?={js.NS})([^*\\n]*?{js.NS})\\*")
_STRIP_TILDE = re.compile(f"~~(?={js.NS})([^~\\n]*?{js.NS})~~")
_CODE = re.compile(r"`([^`\n]+)`")
_HEADING = re.compile(f"{_BOL}[ \\t]*#{{1,6}}[ \\t]+", re.M)
_QUOTE = re.compile(f"{_BOL}[ \\t]*>[ \\t]?", re.M)
_MARKER_RUN = re.compile(f"(^|{js.S})[*_~]{{2,}}(?={js.S}|\\Z)")
_TRAILING_SPACE = re.compile(f"[ \\t]+{_EOL}", re.M)
_DOUBLE_SPACE = re.compile(r"[ \t]{2,}")


def _link(match: re.Match) -> str:
    label, url = match.group(1), match.group(2)
    return url if js.trim(label) == url else f"{js.trim(label)} ({url})"


def clean_prose(value: str | None, keep_emphasis: bool = False) -> str:
    """Turns lightweight Markdown / iCal prose into readable plain text (see text.ts)."""
    if not value:
        return ""
    text = _CRLF.sub("\n", value)
    text = _ZERO_WIDTH.sub("", text)
    text = text.replace(" ", " ")
    text = _ESCAPED.sub(r"\1", text)
    text = _IMAGE.sub(r"\1", text)
    text = _LINK.sub(_link, text)
    text = _BULLET.sub("• ", text)
    if keep_emphasis:
        text = _KEEP_BOLD.sub(r"**\2**", text)
        text = _KEEP_ITALIC.sub(r"\1*\2*", text)
    else:
        text = _STRIP_BOLD.sub(r"\2", text)
        text = _STRIP_STAR.sub(r"\1", text)
        text = _KEEP_ITALIC.sub(r"\1\2", text)
    text = _STRIP_TILDE.sub(r"\1", text)
    text = _CODE.sub(r"\1", text)
    text = _HEADING.sub("", text)
    text = _QUOTE.sub("", text)
    text = _SEPARATOR_LINE.sub("", text)
    text = _MARKER_RUN.sub(r"\1", text)
    text = _renumber_ordered_lists(text)
    text = _TRAILING_SPACE.sub("", text)
    text = _DOUBLE_SPACE.sub(" ", text)
    text = _BLANKS.sub("\n\n", text)
    return js.trim(text)


def _surround(inner: str) -> tuple[str, str, str]:
    # JS `$` without the m flag only matches at the very end.
    match = re.match(f"^({js.S}*)([\\s\\S]*?)({js.S}*)\\Z", inner)
    assert match is not None
    return match.group(1), match.group(2), match.group(3)


def _emphasise(marker: str, inner: str) -> str:
    lead, core, trail = _surround(inner)
    return f"{lead}{marker}{core}{marker}{trail}" if js.trim(_TAG.sub("", core)) else inner


_A_TAG = re.compile(r"<a" + js.S + r"[^>]*href=[\"']([^\"']+)[\"'][^>]*>([\s\S]*?)</a>", re.I | re.A)
_HEADING_TAG = re.compile(r"<(h[1-6])[^>]*>([\s\S]*?)</\1>", re.I | re.A)
_STRONG_TAG = re.compile(r"<(strong|b)(?:" + js.S + r"[^>]*)?>([\s\S]*?)</\1>", re.I | re.A)
_EM_TAG = re.compile(r"<(em|i)(?:" + js.S + r"[^>]*)?>([\s\S]*?)</\1>", re.I | re.A)
_BLOCK_CLOSE = re.compile(r"</(p|div|h[1-6]|ul|ol|blockquote)>", re.I | re.A)


def _anchor(match: re.Match) -> str:
    url = safe_http_url(decode_entities(match.group(1)))
    label = one_line(match.group(2))
    if url and label and label != url:
        return f"{label} ({url})"
    return label or url or ""


def _heading(match: re.Match) -> str:
    label = one_line(match.group(2))
    return f"<p>**{label}**</p>" if label else ""


def html_to_prose(value: str | None) -> str | None:
    """An HTML description (Eventbrite, Fatsoma) in the stored description format."""
    if not value:
        return None
    marked = _A_TAG.sub(_anchor, value)
    marked = _HEADING_TAG.sub(_heading, marked)
    marked = _STRONG_TAG.sub(lambda m: _emphasise("**", m.group(2)), marked)
    marked = _EM_TAG.sub(lambda m: _emphasise("*", m.group(2)), marked)
    marked = _BLOCK_CLOSE.sub(lambda m: f"</{m.group(1)}>\n", marked)
    return or_null(clamp(clean_prose(strip_html(marked), keep_emphasis=True), LIMITS["description"]))


_ALL_SPACE = re.compile(f"{js.S}+")


def one_line(value: str | None) -> str:
    """Single-line version: all whitespace collapsed."""
    return js.trim(_ALL_SPACE.sub(" ", strip_html(value)))


def collapse_space(value: str) -> str:
    """`.replace(/\\s+/g, " ")`."""
    return _ALL_SPACE.sub(" ", value)


def clamp(text: str, limit: int) -> str:
    """Cuts text to `limit` characters (UTF-16 units) on a word boundary, adding "…" when cut."""
    if js.u16len(text) <= limit:
        return text
    cut = js.u16slice(text, limit - 1)
    last_space = cut.rfind(" ")
    if last_space >= 0 and js.u16index(cut, last_space) >= limit * 0.5:
        cut = cut[:last_space]
    return f"{js.trim_end(cut)}…"


def or_null(value: str) -> str | None:
    return value if value else None


def summarise(description: str | None) -> str | None:
    """The first line of a description, short enough for an event card."""
    if not description:
        return None
    first = next((line for line in description.split("\n") if js.u16len(js.trim(line)) > 0), "")
    return or_null(clamp(clean_prose(first), LIMITS["summary"]))


def safe_http_url(value: str | None) -> str | None:
    """Only absolute http(s) URLs survive, WHATWG-normalised."""
    if not value:
        return None
    url = urls.parse(js.trim(value))
    if url is None or url.scheme not in ("http", "https"):
        return None
    href = url.href
    return href if js.u16len(href) <= LIMITS["url"] else None
