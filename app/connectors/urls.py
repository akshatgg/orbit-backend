"""WHATWG URL parsing and serialisation (`new URL(x).toString()`), for http(s) URLs.

Imported events store URLs as the TypeScript importers serialised them: lower-cased
host, default port dropped, `/` for an empty path, spaces and quotes percent-encoded,
dot segments resolved. Comparing a differently normalised URL would rewrite every row,
so this follows the URL Standard for special schemes rather than `urllib`.
Non-special schemes (webcal:, mailto:) only get their scheme read.
"""

import ipaddress
import re
from dataclasses import dataclass, field
from urllib.parse import quote

import idna

SPECIAL_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443, "ftp": 21}

_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*):")
_FORBIDDEN_HOST = set("\x00\t\n\r #/:<>?@[\\]^|")
_FORBIDDEN_DOMAIN = _FORBIDDEN_HOST | {chr(c) for c in range(0x20)} | {"%", "\x7f"}

_C0 = {chr(c) for c in range(0x20)}
_FRAGMENT_SET = _C0 | set(' "<>`')
_QUERY_SET = _C0 | set(' "#<>')
_SPECIAL_QUERY_SET = _QUERY_SET | {"'"}
_PATH_SET = _QUERY_SET | set("?^`{}")
_USERINFO_SET = _PATH_SET | set("/:;=@[\\]^|")


def _encode(text: str, encode_set: set[str]) -> str:
    out = []
    for ch in text:
        if ch in encode_set or ord(ch) > 0x7E:
            out.append("".join(f"%{b:02X}" for b in ch.encode("utf-8", "surrogatepass")))
        else:
            out.append(ch)
    return "".join(out)


def _percent_decode(text: str) -> bytes:
    data = text.encode("utf-8", "surrogatepass")
    out = bytearray()
    i = 0
    while i < len(data):
        if data[i] == 0x25 and i + 2 < len(data) and re.match(rb"[0-9A-Fa-f]{2}", data[i + 1 : i + 3]):
            out.append(int(data[i + 1 : i + 3], 16))
            i += 3
        else:
            out.append(data[i])
            i += 1
    return bytes(out)


def _parse_ipv4_number(part: str) -> int | None:
    if part == "":
        return None
    radix = 10
    if len(part) >= 2 and part[:2] in ("0x", "0X"):
        part, radix = part[2:], 16
    elif len(part) >= 2 and part[0] == "0":
        part, radix = part[1:], 8
    if part == "":
        return 0
    try:
        return int(part, radix) if re.fullmatch(r"[0-9A-Fa-f]+", part) else None
    except ValueError:
        return None


def _ends_in_number(host: str) -> bool:
    parts = host.split(".")
    if parts[-1] == "":
        if len(parts) == 1:
            return False
        parts = parts[:-1]
    last = parts[-1]
    if last and re.fullmatch(r"[0-9]+", last):
        return True
    return _parse_ipv4_number(last) is not None and bool(re.fullmatch(r"0[xX][0-9A-Fa-f]*", last))


def _parse_ipv4(host: str) -> str | None:
    parts = host.split(".")
    if parts[-1] == "" and len(parts) > 1:
        parts = parts[:-1]
    if len(parts) > 4:
        return None
    numbers = []
    for part in parts:
        n = _parse_ipv4_number(part)
        if n is None:
            return None
        numbers.append(n)
    if any(n > 255 for n in numbers[:-1]):
        return None
    if numbers[-1] >= 256 ** (5 - len(numbers)):
        return None
    ipv4 = numbers[-1]
    for i, n in enumerate(numbers[:-1]):
        ipv4 += n * 256 ** (3 - i)
    return ".".join(str((ipv4 >> shift) & 255) for shift in (24, 16, 8, 0))


def _parse_host(raw: str) -> str | None:
    if raw.startswith("["):
        if not raw.endswith("]"):
            return None
        try:
            address = ipaddress.IPv6Address(raw[1:-1])
        except ValueError:
            return None
        return f"[{address.compressed}]"
    domain = _percent_decode(raw).decode("utf-8", "replace")
    try:
        ascii_domain = idna.encode(domain, uts46=True, transitional=False, std3_rules=False).decode("ascii")
    except idna.IDNAError:
        if all(ord(c) < 0x80 for c in domain):
            ascii_domain = domain.lower()
        else:
            return None
    except UnicodeError:
        return None
    if not ascii_domain or any(c in _FORBIDDEN_DOMAIN for c in ascii_domain):
        return None
    if _ends_in_number(ascii_domain):
        return _parse_ipv4(ascii_domain)
    return ascii_domain


def _single_dot(segment: str) -> bool:
    return segment in (".",) or segment.lower() == "%2e"


def _double_dot(segment: str) -> bool:
    return segment.lower() in ("..", ".%2e", "%2e.", "%2e%2e")


@dataclass
class URL:
    scheme: str
    username: str = ""
    password: str = ""
    host: str | None = None
    port: int | None = None
    path: str = ""
    query: str | None = None
    fragment: str | None = None
    special: bool = True
    _opaque: str = field(default="", repr=False)

    @property
    def protocol(self) -> str:
        return f"{self.scheme}:"

    @property
    def hostname(self) -> str:
        return self.host or ""

    @property
    def pathname(self) -> str:
        return self.path

    @property
    def search(self) -> str:
        return f"?{self.query}" if self.query else ""

    @property
    def origin(self) -> str:
        if not self.special or self.scheme == "file":
            return "null"
        port = f":{self.port}" if self.port is not None else ""
        return f"{self.scheme}://{self.host}{port}"

    @property
    def href(self) -> str:
        if not self.special:
            return self._opaque
        userinfo = ""
        if self.username or self.password:
            userinfo = self.username + (f":{self.password}" if self.password else "") + "@"
        port = f":{self.port}" if self.port is not None else ""
        out = f"{self.scheme}://{userinfo}{self.host}{port}{self.path}"
        if self.query is not None:
            out += f"?{self.query}"
        if self.fragment is not None:
            out += f"#{self.fragment}"
        return out

    def __str__(self) -> str:
        return self.href

    @property
    def search_params(self) -> "SearchParams":
        return SearchParams(self)


def parse(value: str) -> URL | None:
    """`new URL(value)`, or None where it throws."""
    if not isinstance(value, str):
        return None
    text = value.strip("".join(chr(c) for c in range(0x21)))
    text = text.replace("\t", "").replace("\n", "").replace("\r", "")
    match = _SCHEME.match(text)
    if not match:
        return None
    scheme = match.group(1).lower()
    rest = text[match.end() :]
    if scheme not in SPECIAL_PORTS:
        return URL(scheme=scheme, special=False, _opaque=f"{scheme}:{rest}")

    rest = rest.lstrip("/\\")
    end = len(rest)
    for i, ch in enumerate(rest):
        if ch in "/\\?#":
            end = i
            break
    authority, rest = rest[:end], rest[end:]

    username = password = ""
    if "@" in authority:
        userinfo, authority = authority.rsplit("@", 1)
        user, _, pw = userinfo.partition(":")
        username = _encode(user, _USERINFO_SET)
        password = _encode(pw, _USERINFO_SET)

    host_text, port_text = authority, ""
    if authority.startswith("["):
        close = authority.find("]")
        if close >= 0 and authority[close + 1 :].startswith(":"):
            host_text, port_text = authority[: close + 1], authority[close + 2 :]
    elif ":" in authority:
        host_text, _, port_text = authority.partition(":")
    if host_text == "":
        return None
    host = _parse_host(host_text)
    if host is None:
        return None
    port: int | None = None
    if port_text:
        if not re.fullmatch(r"[0-9]+", port_text):
            return None
        port = int(port_text)
        if port > 65535:
            return None
        if port == SPECIAL_PORTS[scheme]:
            port = None

    fragment = None
    if "#" in rest:
        rest, fragment = rest.split("#", 1)
        fragment = _encode(fragment, _FRAGMENT_SET)
    query = None
    if "?" in rest:
        rest, query = rest.split("?", 1)
        query = _encode(query, _SPECIAL_QUERY_SET)

    segments: list[str] = []
    raw_segments = re.split(r"[/\\]", rest)[1:] if rest else [""]
    for index, segment in enumerate(raw_segments):
        last = index == len(raw_segments) - 1
        if _double_dot(segment):
            if segments:
                segments.pop()
            if last:
                segments.append("")
        elif _single_dot(segment):
            if last:
                segments.append("")
        else:
            segments.append(_encode(segment, _PATH_SET))
    path = "/" + "/".join(segments)
    return URL(
        scheme=scheme, username=username, password=password, host=host, port=port,
        path=path, query=query, fragment=fragment,
    )


def href(value: str) -> str | None:
    url = parse(value)
    return url.href if url else None


# ─── URLSearchParams ─────────────────────────────────────────────────────────

def _form_decode(text: str) -> str:
    return _percent_decode(text.replace("+", " ")).decode("utf-8", "replace")


def form_encode(text: str) -> str:
    """application/x-www-form-urlencoded byte serialisation (URLSearchParams.toString)."""
    return quote(text.encode("utf-8", "surrogatepass"), safe="*-._ ").replace(" ", "+")


def parse_query(query: str | None) -> list[tuple[str, str]]:
    pairs = []
    for chunk in (query or "").split("&"):
        if not chunk:
            continue
        name, _, value = chunk.partition("=")
        pairs.append((_form_decode(name), _form_decode(value)))
    return pairs


def serialize_query(pairs: list[tuple[str, str]]) -> str:
    return "&".join(f"{form_encode(k)}={form_encode(v)}" for k, v in pairs)


class SearchParams:
    """`url.searchParams`: reads and writes go through to the URL's query."""

    def __init__(self, url: URL | None = None, pairs: list[tuple[str, str]] | None = None) -> None:
        self._url = url
        self.pairs = pairs if pairs is not None else parse_query(url.query if url else None)

    def get(self, name: str) -> str | None:
        for key, value in self.pairs:
            if key == name:
                return value
        return None

    def set(self, name: str, value: str) -> None:
        out, placed = [], False
        for key, current in self.pairs:
            if key == name:
                if not placed:
                    out.append((key, value))
                    placed = True
            else:
                out.append((key, current))
        if not placed:
            out.append((name, value))
        self.pairs = out
        self._update()

    def append(self, name: str, value: str) -> None:
        self.pairs.append((name, value))
        self._update()

    def _update(self) -> None:
        if self._url is not None:
            serialized = serialize_query(self.pairs)
            self._url.query = serialized or None

    def __str__(self) -> str:
        return serialize_query(self.pairs)


def encode_uri_component(text: str) -> str:
    """`encodeURIComponent`."""
    return quote(text.encode("utf-8", "surrogatepass"), safe="-_.!~*'()")
