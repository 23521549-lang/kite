"""Code map: a graphify knowledge graph of the task's repository, turned into prompt text.

graphify (github.com/Graphify-Labs/graphify, ``pip install graphifyy``) parses the code with
tree-sitter, locally and without an LLM or the network (``graphify extract --code-only``), into
``graph.json``: every module, class, function and method with its file and line, the edges
between them (contains, calls, imports, inherits, ...) and the docstrings/comments that explain
them. Kite builds it on the host for the task's clone, outside the workspace (so it never ends up
in the patch), and hands the agent only the parts that matter for the goal:

* where each symbol the goal names is defined, and who calls, imports or inherits it;
* an outline (symbols with line numbers) of a large file the goal names;
* for the self-review: the callers of what the agent changed, so every one stays consistent;
* for a fix round: what each failing test calls.

That is what an agent otherwise spends its first rounds grepping for. The map is a hint, not the
truth: calls through an instance (``Table(x).query()``) are attributed to the class, dynamic
calls are missed. Everything here is optional; without graphify Kite uses the plain file list.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

GRAPHIFY_PACKAGE = "graphifyy==0.9.113"   # the version Kite was checked with (graph.json format)
BUILD_TIMEOUT = 180
CALL_RELATIONS = {"calls", "indirect_call", "references", "uses"}
IMPORT_RELATIONS = {"imports", "imports_from", "re_exports"}
INHERIT_RELATIONS = {"inherits", "extends", "implements", "mixes_in"}
PARENT_RELATIONS = {"method", "contains"}
_IDENT = re.compile(r"`([^`\n]{2,80})`|([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)")


@dataclass(frozen=True)
class Symbol:
    id: str
    name: str        # bare name: query_since, TelemetryEventsTable, tables.py
    kind: str        # module | class | function | method | other
    file: str
    line: int

    @property
    def where(self) -> str:
        return f"{self.file}:{self.line}"

    @property
    def is_test(self) -> bool:
        return is_test_path(self.file)


def is_test_path(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return ("/tests/" in f"/{path}" or "/test/" in f"/{path}" or name.startswith("test_")
            or name.endswith(("_test.py", ".test.js", ".test.ts", ".spec.js", ".spec.ts")) or name == "conftest.py")


def _line(location) -> int:
    match = re.search(r"\d+", str(location or ""))
    return int(match.group()) if match else 0


def graphify_cli() -> list[str] | None:
    """The graphify command next to Kite's own Python (its venv), else on PATH; None if missing."""
    local = Path(sys.executable).with_name("graphify")
    if local.is_file():
        return [str(local)]
    found = shutil.which("graphify")
    return [found] if found else None


def build(workspace: Path, out_dir: Path, *, timeout: int = BUILD_TIMEOUT, runner=subprocess.run,
          cli: list[str] | None = None) -> Path | None:
    """Build (or reuse) ``out_dir/graphify-out/graph.json`` for ``workspace``; None if unavailable.

    ``--code-only`` keeps it local: tree-sitter only, no LLM, no API key, nothing sent anywhere.
    The environment is stripped so no API key could switch on an LLM backend by accident.
    """
    graph = Path(out_dir) / "graphify-out" / "graph.json"
    if graph.is_file():
        return graph
    cli = cli or graphify_cli()
    if not cli:
        return None
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")}
    try:
        result = runner([*cli, "extract", str(workspace), "--code-only", "--out", str(out_dir)],
                        capture_output=True, text=True, timeout=timeout, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    return graph if result.returncode == 0 and graph.is_file() else None


class CodeGraph:
    """The parts of graphify's graph.json Kite uses (node-link format, clustered or raw)."""

    def __init__(self, data: dict, root: Path | None = None):
        self.root = Path(root) if root is not None else None   # the workspace, to find instance calls
        self.symbols: dict[str, Symbol] = {}
        self.by_name: dict[str, list[Symbol]] = defaultdict(list)
        self.by_file: dict[str, list[Symbol]] = defaultdict(list)
        self.into: dict[str, list[tuple[str, str, str, int]]] = defaultdict(list)  # relation, from, file, line
        self.out: dict[str, list[tuple[str, str, str, int]]] = defaultdict(list)
        self.notes: dict[str, str] = {}
        texts: dict[str, str] = {}
        for node in data.get("nodes") or []:
            node_id, label, path = str(node.get("id", "")), str(node.get("label", "")), str(node.get("source_file") or "")
            if not node_id or not path:
                continue
            if node.get("file_type") == "rationale":
                texts[node_id] = label
                continue
            if node.get("file_type") not in (None, "code"):
                continue
            if path.endswith(label) and label.endswith(path.rsplit("/", 1)[-1]) \
                    and _line(node.get("source_location")) <= 1:
                kind, name = "module", path.rsplit("/", 1)[-1]
            elif node.get("_callable_class"):
                kind, name = "class", label
            elif label.endswith("()"):
                kind, name = ("method" if label.startswith(".") else "function"), label.strip(".")[:-2]
            else:
                kind, name = "other", label
            symbol = Symbol(node_id, name, kind, path, _line(node.get("source_location")))
            self.symbols[node_id] = symbol
            self.by_name[name].append(symbol)
            self.by_file[path].append(symbol)
        for edge in data.get("links") or data.get("edges") or []:
            source, target, relation = str(edge.get("source")), str(edge.get("target")), str(edge.get("relation"))
            if relation == "rationale_for":
                if source in texts and target not in self.notes:
                    self.notes[target] = texts[source]
                continue
            if source not in self.symbols or target not in self.symbols:
                continue
            path, line = str(edge.get("source_file") or self.symbols[source].file), _line(edge.get("source_location"))
            self.out[source].append((relation, target, path, line))
            self.into[target].append((relation, source, path, line))
        for symbols in self.by_file.values():
            symbols.sort(key=lambda s: s.line)

    @classmethod
    def load(cls, path: Path | None, root: Path | None = None) -> CodeGraph | None:
        if path is None:
            return None
        try:
            return cls(json.loads(Path(path).read_text(encoding="utf-8")), root)
        except (OSError, ValueError, TypeError, AttributeError):
            return None

    # ------------------------------------------------------------------ lookups
    def parent(self, symbol: Symbol) -> Symbol | None:
        owners = [self.symbols[src] for rel, src, _, _ in self.into[symbol.id] if rel in PARENT_RELATIONS]
        return next((s for s in owners if s.kind == "class"), None) or next(iter(owners), None)

    def qualified(self, symbol: Symbol) -> str:
        owner = self.parent(symbol) if symbol.kind == "method" else None
        name = f"{owner.name}.{symbol.name}" if owner is not None and owner.kind == "class" else symbol.name
        return name + ("()" if symbol.kind in ("function", "method") else "")

    def lookup(self, name: str) -> list[Symbol]:
        """Symbols called ``name`` (``Class.method`` narrows to that class); modules by path too."""
        if "/" in name or name.endswith(".py"):
            return [s for s in self.by_file.get(name, []) if s.kind == "module"]
        owner, _, last = name.rpartition(".")
        found = [s for s in self.by_name.get(last, []) if s.kind != "other"]
        if owner:
            found = [s for s in found if (p := self.parent(s)) is not None and p.name == owner.rsplit(".", 1)[-1]]
        return found

    def at(self, path: str, line: int) -> Symbol | None:
        """The innermost class/function/method starting at or before ``line`` in ``path``."""
        best = None
        for symbol in self.by_file.get(path, []):
            if symbol.kind in ("class", "function", "method") and symbol.line <= line:
                best = symbol
        return best

    def users(self, symbol: Symbol) -> list[tuple[str, Symbol | None, str, int]]:
        """(relation, user, file, line) for every call/import/inheritance pointing at ``symbol``.

        A method called through an instance (``Table(r).query_since(...)``) is an edge to the class
        only, so for methods the files that use the class (and the class's own file) are also
        scanned for ``.name(``.
        """
        seen, found = set(), []
        for relation, source, path, line in self.into[symbol.id]:
            if relation in CALL_RELATIONS | IMPORT_RELATIONS | INHERIT_RELATIONS and (path, line) not in seen:
                seen.add((path, line))
                found.append((relation, self.symbols[source], path, line))
        owner = self.parent(symbol) if symbol.kind == "method" else None
        if owner is None or self.root is None:
            return found
        pattern = re.compile(rf"\.{re.escape(symbol.name)}\s*\(")
        for path in sorted({owner.file} | {p for _, _, p, _ in self.users(owner)}):
            try:
                text = (self.root / path).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if (path, number) in seen or not pattern.search(line):
                    continue
                user = self.at(path, number)
                if user is not None and user.id == symbol.id and number == symbol.line:
                    continue
                seen.add((path, number))
                found.append(("calls", user, path, number))
        return found

    # ------------------------------------------------------------------ text
    def describe(self, symbol: Symbol, *, max_code: int = 8, max_tests: int = 4) -> list[str]:
        note = self.notes.get(symbol.id, "")
        head = f"- {self.qualified(symbol)} — định nghĩa ở {symbol.where}"
        if note:
            head += f' — "{_short(note, 110)}"'
        lines = [head]
        users = self.users(symbol)
        code = [u for u in users if not is_test_path(u[2])]
        tests = [u for u in users if is_test_path(u[2])]
        if code:
            lines.append("  dùng ở: " + " · ".join(_use(u, self) for u in code[:max_code])
                         + (f" · … và {len(code) - max_code} chỗ khác" if len(code) > max_code else ""))
        if tests:
            lines.append("  test dùng: " + " · ".join(_use(u, self) for u in tests[:max_tests])
                         + (f" · … và {len(tests) - max_tests} test khác" if len(tests) > max_tests else ""))
        if not code and not tests:
            lines.append("  (bản đồ không thấy chỗ nào dùng; vẫn nên grep)")
        return lines

    def outline(self, path: str, *, limit: int = 40) -> str:
        """Classes, functions and methods of one file with their line numbers."""
        entries = [s for s in self.by_file.get(path, []) if s.kind in ("class", "function", "method")]
        text = " · ".join(f"L{s.line} {'class ' if s.kind == 'class' else ''}{self.qualified(s)}"
                          for s in entries[:limit])
        return text + (f" · … ({len(entries) - limit} mục nữa)" if len(entries) > limit else "")


def _use(use: tuple[str, Symbol | None, str, int], graph: CodeGraph) -> str:
    relation, user, path, line = use
    where = f"{path}:{line or (user.line if user else 0)}"
    if user is None:
        return where
    if user.kind == "module":
        return f"{where} ({'import' if relation in IMPORT_RELATIONS else relation})"
    return f"{where} ({graph.qualified(user)})"


def _short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def goal_names(goal: str, *, limit: int = 12) -> list[str]:
    """Code-looking names in a (Vietnamese) goal: `quoted`, snake_case, CamelCase, Class.method."""
    names: list[str] = []
    for quoted, bare in _IDENT.findall(goal):
        for token in ([quoted.strip("()")] if quoted else [bare]):
            token = token.strip(".")
            if not token or token in names:
                continue
            parts = token.split(".")
            code_like = bool(quoted) or "." in token and not re.search(r"\.(py|js|ts|md|json|ya?ml|toml|txt|html|css|sh)$", token) \
                or any("_" in p.strip("_") or re.search(r"[a-z][A-Z]", p) for p in parts)
            if code_like and len(token) >= 3 and re.fullmatch(r"[A-Za-z_][\w.]*", token):
                names.append(token)
    return names[:limit]


# Plain words a goal uses that are too common to point at one symbol.
STOP_WORDS = {"test", "tests", "file", "files", "repo", "code", "list", "update", "value", "values", "data",
              "error", "errors", "return", "none", "true", "false", "main", "name", "type", "item", "items",
              "request", "response", "model", "models", "page", "pages", "query", "table", "tables", "user",
              "users", "path", "http", "https", "json", "yaml", "python", "pytest", "commit", "push", "branch",
              "task", "server", "client", "class", "function", "method", "string", "number", "int", "float"}


def goal_words(goal: str, *, limit: int = 12) -> list[str]:
    """Plain ASCII words of a goal that may still name a symbol (``average``, ``median``)."""
    words: list[str] = []
    for word in re.findall(r"(?<![\w.`/-])[A-Za-z][A-Za-z0-9]{3,}(?![\w.`/-])", goal):
        if word.lower() not in STOP_WORDS and word not in words:
            words.append(word)
    return words[:limit]


def goal_map(graph: CodeGraph, goal: str, mentioned_files: list[str], *, max_chars: int = 3500,
             big_file_chars: int = 4000, workspace: Path | None = None) -> tuple[str, list[Symbol]]:
    """The code-map section of the first prompt, and the symbols it found (for excerpts)."""
    lines: list[str] = []
    found: list[Symbol] = []
    names = goal_names(goal)
    plain = [w for w in goal_words(goal) if w not in names]
    for name in names + plain:
        if name in plain:
            # English words are common in Vietnamese goals (usage, tenant, read): a plain word is used
            # only when nothing code-like and no file pointed anywhere, and only for one or two
            # definitions outside the tests.
            if found or mentioned_files:
                break
            matches = [s for s in graph.lookup(name) if s.kind in ("class", "function", "method") and not s.is_test]
            if len(matches) > 2:
                continue
        else:
            matches = graph.lookup(name)
            if not matches and "." in name:      # Class.new_method: the method does not exist yet
                matches = [s for s in graph.lookup(name.rsplit(".", 1)[0]) if s.kind == "class"]
        if not matches:
            continue
        if len(matches) > 3:          # a common name (get, run): too many to be a pointer
            lines.append(f"- {name}: {len(matches)} chỗ định nghĩa, ví dụ "
                         + ", ".join(f"{graph.qualified(s)} ({s.where})" for s in matches[:3]))
            continue
        for symbol in matches:
            if symbol not in found:
                found.append(symbol)
                lines += graph.describe(symbol)
    for path in mentioned_files[:3]:
        size = (Path(workspace) / path).stat().st_size if workspace and (Path(workspace) / path).is_file() else 0
        outline = graph.outline(path)
        if outline and size > big_file_chars:
            lines.append(f"- Mục lục {path}: {outline}")
    text, total = [], 0
    for line in lines:
        if total + len(line) > max_chars:
            text.append("- … (bản đồ bị cắt cho gọn)")
            break
        text.append(line)
        total += len(line) + 1
    return "\n".join(text), found


def changed_lines(workspace: Path, base: str) -> dict[str, list[int]]:
    """Old-side line numbers each tracked file changed at since ``base`` (``git diff -U0``)."""
    result = subprocess.run(["git", "-C", str(workspace), "diff", "-U0", "--no-color", base, "--"],
                            capture_output=True, text=True)
    changed: dict[str, list[int]] = defaultdict(list)
    path = None
    for line in result.stdout.splitlines():
        if line.startswith("--- "):
            path = line[6:] if line.startswith("--- a/") else None
        elif path and line.startswith("@@"):
            match = re.match(r"@@ -(\d+)(?:,(\d+))?", line)
            if match:
                start, count = int(match.group(1)), int(match.group(2) or 1)
                changed[path] += list(range(start, start + max(count, 1)))
    return changed


def impact(graph: CodeGraph, workspace: Path, base: str, *, max_symbols: int = 8, max_chars: int = 3000) -> str:
    """Who uses what the agent changed: for the self-review (keep every caller consistent)."""
    touched: list[Symbol] = []
    for path, lines in changed_lines(workspace, base).items():
        if is_test_path(path):
            continue
        for line in lines:
            symbol = graph.at(path, line)
            if symbol is not None and symbol not in touched:
                touched.append(symbol)
    text: list[str] = []
    for symbol in touched[:max_symbols]:
        block = graph.describe(symbol, max_code=10, max_tests=3)
        if sum(map(len, text)) + sum(map(len, block)) > max_chars:
            break
        text += block
    return "\n".join(text)


def failing_targets(graph: CodeGraph, test_output: str, *, max_tests: int = 5, max_targets: int = 6) -> str:
    """For each failing pytest test: the code it calls (where to look first)."""
    lines: list[str] = []
    seen = set()
    for path, name in re.findall(r"(?:FAILED|ERROR) ([\w./-]+\.py)::(?:\w+::)*?(\w+)(?:\[|\s|$)", test_output):
        if (path, name) in seen or len(lines) >= max_tests:
            continue
        seen.add((path, name))
        test = next((s for s in graph.by_file.get(path, []) if s.name == name), None)
        if test is None:
            continue
        targets, names = [], set()
        for relation, target, _, _ in graph.out[test.id]:
            symbol = graph.symbols[target]
            if relation in CALL_RELATIONS and not symbol.is_test and symbol.kind != "module" and symbol.id not in names:
                names.add(symbol.id)
                targets.append(f"{graph.qualified(symbol)} ({symbol.where})")
        if targets:
            lines.append(f"- {name} gọi: " + ", ".join(targets[:max_targets]))
    return "\n".join(lines)
