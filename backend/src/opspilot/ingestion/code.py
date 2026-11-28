"""Chunking source code along its structure: one chunk per function, method or class.

A passage cut from the middle of a function answers nothing ("what does retry_payment do?"),
so code is split where a programmer would split it. Each chunk's heading names it
("PaymentClient.retry"), which is also what keyword search matches on.

- **Python** is parsed with the standard library's `ast`: exact boundaries, decorators and the
  comments right above a definition included. Classes too big for one chunk become their
  header (with the docstring and attributes) plus one chunk per method.
- **Brace languages** (JavaScript/TypeScript, Go, Java, Kotlin, C#, Rust, PHP, shell) are
  split by declarations found with per-language patterns, each extended to its matching
  closing brace. Strings and comments are skipped while counting braces.
- **Ruby** blocks end at the `end` that matches their indentation.
- Anything else, or code that doesn't parse, falls back to windows of whole lines.

Code between definitions (imports, constants, `main`) is kept as "(module)" chunks, so nothing
in the file is unsearchable. Every chunk is an exact slice of the file, like prose chunks.
"""

import ast
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from opspilot.ingestion.chunking import ChunkDraft, estimate_tokens

LANGUAGES = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".java": "java",
    ".kt": "kotlin",
    ".cs": "csharp",
    ".rs": "rust",
    ".php": "php",
    ".rb": "ruby",
    ".sh": "shell",
    ".bash": "shell",
}
CODE_MEDIA_PREFIX = "text/x-"


def language_of(filename: str) -> str | None:
    return LANGUAGES.get(PurePosixPath(filename).suffix.lower())


def media_type_for(language: str) -> str:
    return f"{CODE_MEDIA_PREFIX}{language}"


def language_of_media_type(media_type: str) -> str | None:
    if not media_type.startswith(CODE_MEDIA_PREFIX):
        return None
    language = media_type.removeprefix(CODE_MEDIA_PREFIX)
    return language if language in LANGUAGES.values() else None


@dataclass
class _Unit:
    name: str
    start: int  # first line, 0-based
    end: int  # last line, inclusive


class _Lines:
    """Line numbers ↔ character offsets, so chunks are exact slices of the text."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.lines = text.splitlines(keepends=True)
        self.offsets = [0]
        for line in self.lines:
            self.offsets.append(self.offsets[-1] + len(line))

    def span(self, start: int, end: int) -> tuple[int, int]:
        return self.offsets[start], self.offsets[end + 1]

    def blank(self, i: int) -> bool:
        return not self.lines[i].strip()


# --------------------------------------------------------------------------- python --


def _python_units(text: str, lines: _Lines, max_tokens: int) -> list[_Unit] | None:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None

    def start_of(node: ast.stmt) -> int:
        decorators: list[ast.expr] = getattr(node, "decorator_list", [])
        first = min([node.lineno, *(d.lineno for d in decorators)]) - 1
        # Comments directly above belong to the definition
        while first > 0 and lines.lines[first - 1].lstrip().startswith("#"):
            first -= 1
        return first

    units: list[_Unit] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        start, end = start_of(node), (node.end_lineno or node.lineno) - 1
        size = estimate_tokens(text[slice(*lines.span(start, end))])
        methods = (
            [m for m in node.body if isinstance(m, ast.FunctionDef | ast.AsyncFunctionDef)]
            if isinstance(node, ast.ClassDef)
            else []
        )
        if size <= max_tokens or not methods:
            units.append(_Unit(node.name, start, end))
            continue
        # Too big: the class header (docstring, attributes), then each method
        header_end = start_of(methods[0]) - 1
        units.append(_Unit(node.name, start, max(start, header_end)))
        for method in methods:
            units.append(
                _Unit(
                    f"{node.name}.{method.name}",
                    start_of(method),
                    (method.end_lineno or method.lineno) - 1,
                )
            )
    return units


# ------------------------------------------------------------------- brace languages --

_IDENT = r"[A-Za-z_$][\w$]*"
_DECLARATIONS: dict[str, list[re.Pattern[str]]] = {
    "javascript": [
        re.compile(rf"^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*({_IDENT})"),
        re.compile(rf"^(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+({_IDENT})"),
        re.compile(
            rf"^(?:export\s+)?(?:const|let|var)\s+({_IDENT})\s*(?::[^=]+)?=\s*"
            rf"(?:async\s+)?(?:function\b|\([^)]*\)\s*(?::[^=]+)?=>|{_IDENT}\s*=>)"
        ),
        re.compile(rf"^(?:export\s+)?(?:declare\s+)?(?:interface|enum|namespace)\s+({_IDENT})"),
        re.compile(rf"^(?:export\s+)?type\s+({_IDENT})\s*(?:<[^>]*>)?\s*=\s*\{{"),
        # Class members: methods, getters, constructors
        re.compile(
            rf"^\s{{2,4}}(?:(?:public|private|protected|static|async|readonly|override|get|set)\s+)*"
            rf"\*?({_IDENT})\s*(?:<[^>]*>)?\([^;]*\)\s*(?::[^={{;]+)?\{{\s*$"
        ),
    ],
    "go": [
        re.compile(rf"^func\s+(?:\((?:\w+\s+)?\*?({_IDENT})[^)]*\)\s*)?({_IDENT})"),
        re.compile(rf"^type\s+({_IDENT})\s+(?:struct|interface)\b"),
    ],
    "java": [
        re.compile(
            rf"^\s{{0,4}}(?:@\w+\s+)*(?:(?:public|private|protected|static|final|abstract|sealed)\s+)*"
            rf"(?:class|interface|enum|record)\s+({_IDENT})"
        ),
        re.compile(
            rf"^\s{{1,4}}(?:(?:public|private|protected|static|final|abstract|synchronized|default)\s+)*"
            rf"[\w<>\[\],.? ]+\s+({_IDENT})\s*\([^;]*$"
        ),
    ],
    "kotlin": [
        re.compile(
            rf"^\s{{0,4}}(?:(?:public|private|internal|protected|open|abstract|data|sealed|enum|"
            rf"inline|value|annotation)\s+)*(?:class|interface|object)\s+({_IDENT})"
        ),
        re.compile(
            rf"^\s{{0,4}}(?:(?:public|private|internal|protected|open|override|suspend|inline|"
            rf"operator|infix|tailrec)\s+)*fun\s+(?:<[^>]+>\s*)?(?:[\w.]+\.)?({_IDENT})"
        ),
    ],
    "csharp": [
        re.compile(
            rf"^\s{{0,8}}(?:(?:public|private|protected|internal|static|sealed|abstract|partial)\s+)*"
            rf"(?:class|interface|struct|enum|record)\s+({_IDENT})"
        ),
        re.compile(
            rf"^\s{{4,8}}(?:(?:public|private|protected|internal|static|virtual|override|async|"
            rf"sealed|abstract)\s+)+[\w<>\[\],.? ]+\s+({_IDENT})\s*\([^;]*$"
        ),
    ],
    "rust": [
        re.compile(
            rf"^(?:pub(?:\([\w:]+\))?\s+)?(?:async\s+)?(?:unsafe\s+)?(?:const\s+)?"
            rf"(?:fn|struct|enum|trait|mod)\s+({_IDENT})"
        ),
        re.compile(rf"^impl(?:<[^>]*>)?\s+(?:[\w:<>]+\s+for\s+)?({_IDENT})"),
    ],
    "php": [
        re.compile(
            rf"^\s{{0,4}}(?:(?:public|private|protected|static|final|abstract)\s+)*"
            rf"(?:function|class|interface|trait)\s+({_IDENT})"
        ),
    ],
    "shell": [
        re.compile(rf"^(?:function\s+)?({_IDENT})\s*\(\)\s*\{{?"),
        re.compile(rf"^function\s+({_IDENT})\s*\{{?"),
    ],
}
_DECLARATIONS["typescript"] = _DECLARATIONS["javascript"]
_PREFIX_LINE = re.compile(r"^\s*(?://|/\*|\*|#(?!!)|@\w|\[\w)")
_STRING = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`')


# Look like method declarations to the patterns above, but aren't
_KEYWORDS = frozenset(
    {
        "if",
        "for",
        "while",
        "switch",
        "catch",
        "return",
        "function",
        "else",
        "do",
        "try",
        "with",
        "new",
    }
)


def _match(language: str, line: str) -> str | None:
    for pattern in _DECLARATIONS[language]:
        if m := pattern.match(line):
            names = [g for g in m.groups() if g]
            if names and names[-1] in _KEYWORDS:
                continue
            if language == "go" and len(names) == 2:
                return f"{names[0]}.{names[1]}"  # a method: Receiver.Name
            return names[-1] if names else None
    return None


def _brace_end(lines: list[str], start: int) -> int:
    """The line where the block opened at or after `start` closes."""
    depth = 0
    opened = False
    in_block_comment = False
    for i in range(start, len(lines)):
        line = lines[i]
        if in_block_comment:
            if "*/" not in line:
                continue
            line = line.split("*/", 1)[1]
            in_block_comment = False
        line = _STRING.sub('""', line)
        line = re.sub(r"/\*.*?\*/", "", line)
        if "/*" in line:
            line, in_block_comment = line.split("/*", 1)[0], True
        line = line.split("//", 1)[0]
        for ch in line:
            if ch == "{":
                depth += 1
                opened = True
            elif ch == "}":
                depth -= 1
        if opened and depth <= 0:
            return i
        # A declaration without a body (an arrow returning an expression, a prototype)
        if not opened and i > start and line.rstrip().endswith(";"):
            return i
        if not opened and i == start and line.rstrip().endswith(";"):
            return i
    return len(lines) - 1


def _ruby_end(lines: list[str], start: int) -> int:
    indent = len(lines[start]) - len(lines[start].lstrip())
    for i in range(start + 1, len(lines)):
        stripped = lines[i].strip()
        if stripped == "end" and len(lines[i]) - len(lines[i].lstrip()) == indent:
            return i
    return len(lines) - 1


_RUBY = re.compile(r"^(\s*)(?:def\s+(?:self\.)?|class\s+|module\s+)([\w:?!=]+)")


def _pattern_units(language: str, lines: _Lines) -> list[_Unit]:
    units: list[_Unit] = []
    i = 0
    raw = lines.lines
    owner: tuple[str, int] | None = None  # enclosing class and where it ends
    while i < len(raw):
        line = raw[i]
        if language == "ruby":
            m = _RUBY.match(line)
            name = m.group(2) if m else None
        else:
            name = _match(language, line)
        if name is None:
            i += 1
            continue
        start = i
        while start > 0 and _PREFIX_LINE.match(raw[start - 1]) and not lines.blank(start - 1):
            start -= 1
        end = _ruby_end(raw, i) if language == "ruby" else _brace_end(raw, i)
        indented = line[:1].isspace()
        if owner and i <= owner[1] and indented:
            name = f"{owner[0]}.{name}"
        units.append(_Unit(name, start, end))
        if re.search(r"\b(class|interface|object|impl|module|struct|trait|record)\b", line):
            # Members are found inside it too; remember the owner for their names
            owner = (name.split(".")[-1], end)
            i += 1
        else:
            i = end + 1
    return units


# ------------------------------------------------------------------------- assembly --


def _windows(lines: _Lines, start: int, end: int, max_tokens: int) -> list[tuple[int, int]]:
    """Whole-line windows of at most max_tokens over [start, end]."""
    out: list[tuple[int, int]] = []
    budget = max_tokens * 4  # characters
    first = start
    size = 0
    for i in range(start, end + 1):
        length = len(lines.lines[i])
        if size and size + length > budget:
            out.append((first, i - 1))
            first, size = i, 0
        size += length
    if first <= end:
        out.append((first, end))
    return out


def _flatten(units: list[_Unit]) -> list[_Unit]:
    """Drops units nested in another (a class kept whole contains its methods)."""
    units = sorted(units, key=lambda u: (u.start, -u.end))
    kept: list[_Unit] = []
    for unit in units:
        parent = kept[-1] if kept else None
        if parent and unit.start >= parent.start and unit.end <= parent.end:
            continue
        kept.append(unit)
    return kept


def chunk_code(text: str, language: str, max_tokens: int) -> list[ChunkDraft]:
    lines = _Lines(text)
    if not lines.lines:
        return []
    units = _python_units(text, lines, max_tokens) if language == "python" else None
    if units is None:
        units = (
            _pattern_units(language, lines)
            if language in _DECLARATIONS or language == "ruby"
            else []
        )
    if language != "python":
        # Keep a whole class when it fits; its members otherwise
        whole: list[_Unit] = []
        for unit in sorted(units, key=lambda u: (u.start, -u.end)):
            if "." not in unit.name:
                whole.append(unit)
                continue
            parent = next((w for w in whole if w.start <= unit.start and unit.end <= w.end), None)
            if parent is None:
                whole.append(unit)
                continue
            size = estimate_tokens(text[slice(*lines.span(parent.start, parent.end))])
            if size > max_tokens:
                whole.append(unit)
        units = whole
        # A class split into members: its own chunk is the header, up to the first member
        split = [u for u in units if any(m.name.startswith(f"{u.name}.") for m in units)]
        for parent in split:
            members = [m for m in units if m.name.startswith(f"{parent.name}.")]
            last = max(members, key=lambda m: m.end)
            # The class's closing lines go with its last member, not into a stray chunk
            last.end = max(last.end, parent.end)
            parent.end = max(parent.start, min(m.start for m in members) - 1)
    units = _flatten(units)

    spans: list[tuple[str, int, int]] = []
    cursor = 0
    for unit in units:
        if unit.start < cursor:
            unit.start = cursor
        if unit.start > unit.end:
            continue
        if any(not lines.blank(i) for i in range(cursor, unit.start)):
            spans.append(("(module)", cursor, unit.start - 1))
        spans.append((unit.name, unit.start, unit.end))
        cursor = unit.end + 1
    if cursor < len(lines.lines) and any(
        not lines.blank(i) for i in range(cursor, len(lines.lines))
    ):
        spans.append(("(module)", cursor, len(lines.lines) - 1))

    drafts: list[ChunkDraft] = []
    for name, start, end in spans:
        # Trim blank lines at the edges: they'd only pad the chunk
        while start < end and lines.blank(start):
            start += 1
        while end > start and lines.blank(end):
            end -= 1
        pieces = _windows(lines, start, end, max_tokens)
        for n, (a, b) in enumerate(pieces):
            char_start, char_end = lines.span(a, b)
            content = text[char_start:char_end]
            if not content.strip():
                continue
            heading = name if len(pieces) == 1 else f"{name} (part {n + 1} of {len(pieces)})"
            drafts.append(
                ChunkDraft(
                    ordinal=len(drafts),
                    heading=heading,
                    content=content,
                    token_count=estimate_tokens(content),
                    char_start=char_start,
                    char_end=char_end,
                )
            )
    return drafts
