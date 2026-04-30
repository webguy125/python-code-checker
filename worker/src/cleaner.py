from __future__ import annotations

import ast
import re
import tokenize
from dataclasses import dataclass
from io import StringIO


_SIMPLE_DOUBLE_QUOTE_RE = re.compile(r'^([rRuUbBfF]{0,2})"([^"\\\n]*)"$')


@dataclass(frozen=True)
class Issue:
    type: str
    message: str
    line: int | None = None
    column: int | None = None

    def to_dict(self) -> dict[str, int | str]:
        payload: dict[str, int | str] = {
            "type": self.type,
            "message": self.message,
        }
        if self.line is not None:
            payload["line"] = self.line
        if self.column is not None:
            payload["column"] = self.column
        return payload


def clean_python_code(source: str) -> tuple[str, list[dict[str, int | str]]]:
    issues: list[Issue] = []
    _collect_source_issues(source, issues)

    tree = ast.parse(source)
    tree, import_issues = _normalize_imports(tree)
    issues.extend(import_issues)

    ast.fix_missing_locations(tree)
    cleaned = ast.unparse(tree)
    cleaned, quote_issues = _normalize_quotes(cleaned)
    issues.extend(quote_issues)

    if not cleaned.endswith("\n"):
        cleaned += "\n"
        issues.append(Issue("newline", "Ensured a final newline."))

    return cleaned, [issue.to_dict() for issue in issues]


def _collect_source_issues(source: str, issues: list[Issue]) -> None:
    source = source.replace("\r\n", "\n").replace("\r", "\n")
    lines = source.split("\n")
    protected_lines = _multiline_string_lines(source)
    trailing_whitespace_removed = False
    tabs_in_indentation = False
    consecutive_blank_lines = False

    for line_number, line in enumerate(lines, start=1):
        if line_number in protected_lines:
            consecutive_blank_lines = False
            continue

        if line.rstrip() != line:
            trailing_whitespace_removed = True

        if line.strip() == "":
            if consecutive_blank_lines:
                issues.append(Issue("blank_lines", "Compressed consecutive blank lines."))
                break
            consecutive_blank_lines = True
            continue

        consecutive_blank_lines = False
        leading = len(line) - len(line.lstrip(" \t"))
        if leading and "\t" in line[:leading]:
            tabs_in_indentation = True

    if trailing_whitespace_removed:
        issues.append(Issue("trailing_whitespace", "Removed trailing whitespace."))

    if tabs_in_indentation:
        issues.append(Issue("tabs", "Converted tabs to 4 spaces in indentation."))


def _normalize_imports(tree: ast.Module) -> tuple[ast.Module, list[Issue]]:
    body = tree.body
    if not body:
        return tree, []

    index = 0
    new_body: list[ast.stmt] = []
    issues: list[Issue] = []

    if _is_docstring(body[0]):
        new_body.append(body[0])
        index = 1

    import_nodes: list[ast.stmt] = []
    while index < len(body) and isinstance(body[index], (ast.Import, ast.ImportFrom)):
        import_nodes.append(body[index])
        index += 1

    if not import_nodes:
        return tree, []

    normalized_import_nodes = _rebuild_import_block(import_nodes)
    original = [ast.unparse(node) for node in import_nodes]
    rebuilt = [ast.unparse(node) for node in normalized_import_nodes]
    if original != rebuilt:
        issues.append(Issue("imports", "Sorted and deduplicated top-level imports."))

    new_body.extend(normalized_import_nodes)
    new_body.extend(body[index:])
    tree.body = new_body
    return tree, issues


def _rebuild_import_block(nodes: list[ast.stmt]) -> list[ast.stmt]:
    future_names: set[tuple[str, str | None]] = set()
    plain_imports: set[tuple[str, str | None]] = set()
    from_imports: dict[tuple[int, str | None], set[tuple[str, str | None]]] = {}

    for node in nodes:
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            for alias in node.names:
                future_names.add((alias.name, alias.asname))
            continue

        if isinstance(node, ast.Import):
            for alias in node.names:
                plain_imports.add((alias.name, alias.asname))
            continue

        if isinstance(node, ast.ImportFrom):
            key = (node.level, node.module)
            aliases = from_imports.setdefault(key, set())
            for alias in node.names:
                aliases.add((alias.name, alias.asname))

    rebuilt: list[ast.stmt] = []

    if future_names:
        future_aliases = [
            ast.alias(name=name, asname=asname)
            for name, asname in sorted(future_names, key=lambda item: (item[0].lower(), item[1] or ""))
        ]
        rebuilt.append(ast.parse(_import_stmt_from_future(future_aliases)).body[0])

    for name, asname in sorted(plain_imports, key=lambda item: (item[0].lower(), item[1] or "")):
        rebuilt.append(ast.parse(_import_stmt_import(name, asname)).body[0])

    for (level, module), aliases in sorted(
        from_imports.items(),
        key=lambda item: (
            item[0][0],
            (item[0][1] or "").lower(),
        ),
    ):
        alias_nodes = [
            ast.alias(name=name, asname=asname)
            for name, asname in sorted(aliases, key=lambda item: (item[0].lower(), item[1] or ""))
        ]
        rebuilt.append(ast.parse(_import_stmt_from(level, module, alias_nodes)).body[0])

    return rebuilt


def _import_stmt_import(name: str, asname: str | None) -> str:
    if asname:
        return f"import {name} as {asname}"
    return f"import {name}"


def _import_stmt_from(level: int, module: str | None, aliases: list[ast.alias]) -> str:
    prefix = "." * level
    module_part = f"{prefix}{module}" if module else prefix
    names = ", ".join(_alias_to_source(alias) for alias in aliases)
    return f"from {module_part} import {names}"


def _import_stmt_from_future(aliases: list[ast.alias]) -> str:
    names = ", ".join(_alias_to_source(alias) for alias in aliases)
    return f"from __future__ import {names}"


def _alias_to_source(alias: ast.alias) -> str:
    if alias.asname:
        return f"{alias.name} as {alias.asname}"
    return alias.name


def _normalize_quotes(source: str) -> tuple[str, list[Issue]]:
    changed = False
    tokens: list[tokenize.TokenInfo] = []

    for token in tokenize.generate_tokens(StringIO(source).readline):
        if token.type == tokenize.STRING:
            replacement = _maybe_single_quote(token.string)
            if replacement != token.string:
                changed = True
                token = token._replace(string=replacement)
        tokens.append(token)

    if not changed:
        return source, []

    return tokenize.untokenize(tokens), [
        Issue("quotes", "Normalized simple string quotes to single quotes.")
    ]


def _maybe_single_quote(token_string: str) -> str:
    match = _SIMPLE_DOUBLE_QUOTE_RE.match(token_string)
    if not match:
        return token_string

    prefix = match.group(1)
    body = match.group(2)

    if "'" in body:
        return token_string

    return f"{prefix}'{body}'"


def _is_docstring(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _multiline_string_lines(source: str) -> set[int]:
    protected: set[int] = set()
    for token in tokenize.generate_tokens(StringIO(source).readline):
        if token.type == tokenize.STRING and token.start[0] != token.end[0]:
            protected.update(range(token.start[0], token.end[0] + 1))
    return protected
