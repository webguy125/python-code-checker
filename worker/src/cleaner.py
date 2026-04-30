from __future__ import annotations

import ast
import re
import tokenize
from dataclasses import dataclass
from io import StringIO


_SIMPLE_DOUBLE_QUOTE_RE = re.compile(r'^([rRuUbBfF]{0,2})"([^"\\\n]*)"$')
_BLOCK_HEADER_RE = re.compile(
    r"^(async\s+def|def|class|if|elif|else|for|while|try|except|finally|with|match|case)\b"
)
_DEDENT_HEADER_RE = re.compile(r"^(elif|else|except|finally|case)\b")


@dataclass(frozen=True)
class Issue:
    type: str
    message: str
    line: int | None = None
    column: int | None = None
    severity: str = "medium"

    def to_dict(self) -> dict[str, int | str]:
        payload: dict[str, int | str] = {
            "type": self.type,
            "message": self.message,
            "severity": self.severity,
        }
        if self.line is not None:
            payload["line"] = self.line
        if self.column is not None:
            payload["column"] = self.column
        return payload


def clean_python_code(source: str) -> tuple[str, list[dict[str, int | str]]]:
    issues: list[Issue] = []
    normalized = _normalize_source(source, issues)

    parsed_tree: ast.Module | None = None
    candidate = normalized

    try:
        parsed_tree = ast.parse(candidate)
    except SyntaxError:
        candidate, repair_issues = _repair_broken_python(candidate)
        issues.extend(repair_issues)
        try:
            parsed_tree = ast.parse(candidate)
        except SyntaxError as exc:
            issues.append(
                Issue(
                    "syntax_error",
                    f"Unable to fully repair the code: {exc.msg}",
                    line=exc.lineno,
                    column=exc.offset,
                    severity="high",
                )
            )
            candidate = _ensure_trailing_newline(candidate, issues)
            return candidate, [issue.to_dict() for issue in issues]

    parsed_tree, import_issues = _normalize_imports(parsed_tree)
    issues.extend(import_issues)
    parsed_tree, return_issues = _repair_missing_fallback_returns(parsed_tree)
    issues.extend(return_issues)

    ast.fix_missing_locations(parsed_tree)
    cleaned = ast.unparse(parsed_tree)
    cleaned, quote_issues = _normalize_quotes(cleaned)
    issues.extend(quote_issues)
    cleaned = _ensure_trailing_newline(cleaned, issues)
    return cleaned, [issue.to_dict() for issue in issues]


def _normalize_source(source: str, issues: list[Issue]) -> str:
    source = source.replace("\r\n", "\n").replace("\r", "\n")
    lines = source.split("\n")
    protected_lines = _multiline_string_lines(source)
    normalized_lines: list[str] = []
    saw_trailing_whitespace = False
    saw_tabs = False
    compressed_blank_lines = False
    blank_run = 0

    for line_number, line in enumerate(lines, start=1):
        if line_number in protected_lines:
            normalized_lines.append(line)
            blank_run = 0 if line.strip() else blank_run + 1
            continue

        if line.startswith(r"\t"):
            saw_tabs = True
            literal_tabs = len(line) - len(line.lstrip("\\t"))
            line = ("    " * literal_tabs) + line[literal_tabs:]

        if line.rstrip() != line:
            saw_trailing_whitespace = True
        line = line.rstrip()

        leading = len(line) - len(line.lstrip(" \t"))
        if leading and "\t" in line[:leading]:
            saw_tabs = True
            line = line[:leading].replace("\t", "    ") + line[leading:]

        if line.strip() == "":
            blank_run += 1
            if blank_run > 2:
                compressed_blank_lines = True
                continue
        else:
            blank_run = 0

        normalized_lines.append(line)

    if saw_trailing_whitespace:
        issues.append(Issue("trailing_whitespace", "Removed trailing whitespace."))
    if saw_tabs:
        issues.append(Issue("tabs", "Converted indentation tabs to spaces."))
    if compressed_blank_lines:
        issues.append(Issue("blank_lines", "Compressed excessive blank lines."))

    return "\n".join(normalized_lines)


def _repair_broken_python(source: str) -> tuple[str, list[Issue]]:
    issues: list[Issue] = []
    lines = source.split("\n")
    repaired_lines: list[str] = []
    indent_level = 0
    compressed_blank_lines = False
    quote_repairs = False
    colon_repairs = False
    indentation_repairs = False
    import_dedupe_repairs = False
    blank_run = 0

    in_triple_string = False
    triple_delimiter = ""

    for index, original_line in enumerate(lines, start=1):
        toggled = _toggle_triple_quote_state(original_line, in_triple_string, triple_delimiter)
        if toggled:
            in_triple_string, triple_delimiter = toggled
            repaired_lines.append(original_line.rstrip())
            blank_run = 0 if original_line.strip() else blank_run + 1
            continue

        stripped = original_line.strip()
        if stripped == "":
            blank_run += 1
            if blank_run <= 2:
                repaired_lines.append("")
            else:
                compressed_blank_lines = True
            continue

        blank_run = 0
        original_line = _replace_leading_literal_tabs(original_line)
        content = original_line.lstrip(" \t").rstrip()

        fixed_quotes = _fix_simple_quote_mismatch(content)
        if fixed_quotes != content:
            quote_repairs = True
            content = fixed_quotes

        fixed_colon = _add_missing_colon(content)
        if fixed_colon != content:
            colon_repairs = True
            content = fixed_colon

        original_indent_units = _indent_units(original_line)
        if original_indent_units == 0:
            indent_level = 0
        elif original_indent_units < indent_level and not _DEDENT_HEADER_RE.match(content):
            indent_level = original_indent_units

        if _DEDENT_HEADER_RE.match(content):
            indent_level = max(indent_level - 1, 0)

        desired_indent = " " * (indent_level * 4)
        if desired_indent + content != original_line.rstrip():
            indentation_repairs = True
        repaired_lines.append(f"{desired_indent}{content}")

        if _BLOCK_HEADER_RE.match(content) and content.endswith(":"):
            indent_level += 1

    repaired_source = "\n".join(repaired_lines)
    repaired_source, imports_changed = _dedupe_plain_import_lines(repaired_source)
    if imports_changed:
        import_dedupe_repairs = True

    if indentation_repairs:
        issues.append(Issue("indentation", "Rebuilt indentation to produce parseable Python."))
    if colon_repairs:
        issues.append(Issue("missing_colons", "Inserted missing block colons where the syntax was obvious."))
    if quote_repairs:
        issues.append(Issue("quotes", "Balanced simple mismatched quotes in broken string literals."))
    if compressed_blank_lines:
        issues.append(Issue("blank_lines", "Compressed excessive blank lines during repair."))
    if import_dedupe_repairs:
        issues.append(Issue("imports", "Deduplicated repeated plain import statements during repair."))

    return repaired_source, issues


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


def _repair_missing_fallback_returns(tree: ast.Module) -> tuple[ast.Module, list[Issue]]:
    issues: list[Issue] = []

    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        fallback_name = _find_fallback_return_name(node)
        if not fallback_name:
            continue

        node.body.append(
            ast.Return(
                value=ast.Name(id=fallback_name, ctx=ast.Load())
            )
        )
        issues.append(
            Issue(
                "returns",
                f"Added a fallback return for '{fallback_name}' to keep the function return path consistent.",
                severity="medium",
            )
        )

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
        key=lambda item: (item[0][0], (item[0][1] or "").lower()),
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


def _fix_simple_quote_mismatch(line: str) -> str:
    quote_positions = [(idx, char) for idx, char in enumerate(line) if char in {"'", '"'}]
    if len(quote_positions) != 2:
        return line

    first_index, first_quote = quote_positions[0]
    last_index, last_quote = quote_positions[1]
    if first_quote == last_quote:
        return line

    middle = line[first_index + 1:last_index]
    if "'" in middle or '"' in middle:
        return line

    return f"{line[:last_index]}{first_quote}{line[last_index + 1:]}"


def _add_missing_colon(line: str) -> str:
    stripped = line.rstrip()
    if stripped.endswith(":") or not _BLOCK_HEADER_RE.match(stripped):
        return line

    if stripped.endswith(")") or re.search(r"\b(else|try|finally)\b$", stripped) or stripped.startswith("except"):
        return f"{stripped}:"

    if stripped.startswith(("if ", "elif ", "for ", "while ", "with ", "match ", "case ", "class ", "async def ", "def ")):
        return f"{stripped}:"

    return line


def _dedupe_plain_import_lines(source: str) -> tuple[str, bool]:
    lines = source.split("\n")
    seen: set[str] = set()
    updated: list[str] = []
    changed = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("import ") and " from " not in stripped:
            normalized = re.sub(r"\s+", " ", stripped)
            if normalized in seen:
                changed = True
                continue
            seen.add(normalized)
        updated.append(line)

    return "\n".join(updated), changed


def _ensure_trailing_newline(source: str, issues: list[Issue]) -> str:
    if source.endswith("\n"):
        return source
    issues.append(Issue("newline", "Ensured a final newline.", severity="low"))
    return source + "\n"


def _is_docstring(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _find_fallback_return_name(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    if not node.body or isinstance(node.body[-1], ast.Return):
        return None

    last_stmt = node.body[-1]
    if not isinstance(last_stmt, ast.If):
        return None

    if last_stmt.orelse:
        return None

    returned_name = _single_returned_name(last_stmt.body)
    if not returned_name:
        return None

    assigned_before = _last_assigned_name_before_if(node.body[:-1])
    if assigned_before != returned_name:
        return None

    return returned_name


def _single_returned_name(statements: list[ast.stmt]) -> str | None:
    if len(statements) < 1:
        return None

    last_stmt = statements[-1]
    if not isinstance(last_stmt, ast.Return):
        return None

    value = last_stmt.value
    if not isinstance(value, ast.Name):
        return None

    for stmt in statements[:-1]:
        if isinstance(stmt, (ast.Return, ast.Raise, ast.Break, ast.Continue)):
            return None

    return value.id


def _last_assigned_name_before_if(statements: list[ast.stmt]) -> str | None:
    for stmt in reversed(statements):
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            return stmt.targets[0].id
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            return stmt.target.id
        if isinstance(stmt, (ast.Return, ast.Raise)):
            return None
    return None


def _multiline_string_lines(source: str) -> set[int]:
    protected: set[int] = set()
    try:
        for token in tokenize.generate_tokens(StringIO(source).readline):
            if token.type == tokenize.STRING and token.start[0] != token.end[0]:
                protected.update(range(token.start[0], token.end[0] + 1))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return set()
    return protected


def _toggle_triple_quote_state(line: str, in_triple_string: bool, current_delimiter: str) -> tuple[bool, str] | None:
    delimiter = current_delimiter
    state = in_triple_string
    for token in ('"""', "'''"):
        count = line.count(token)
        if count % 2 == 1:
            if state and delimiter == token:
                state = False
                delimiter = ""
            elif not state:
                state = True
                delimiter = token
    if state == in_triple_string and delimiter == current_delimiter:
        return None
    return state, delimiter


def _indent_units(line: str) -> int:
    line = _replace_leading_literal_tabs(line)
    expanded = line.replace("\t", "    ")
    leading_spaces = len(expanded) - len(expanded.lstrip(" "))
    if leading_spaces <= 0:
        return 0
    return max(1, leading_spaces // 4)


def _replace_leading_literal_tabs(line: str) -> str:
    count = 0
    remaining = line
    while remaining.startswith(r"\t"):
        count += 1
        remaining = remaining[2:]
    if count == 0:
        return line
    return ("    " * count) + remaining
