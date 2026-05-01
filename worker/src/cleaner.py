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
_SNAKE_CASE_EDGE_RE = re.compile(r"(?<!^)(?=[A-Z])")
_TERMINAL_NODES = (ast.Return, ast.Raise, ast.Continue, ast.Break)


@dataclass(frozen=True)
class Issue:
    type: str
    message: str
    line: int | None = None
    column: int | None = None
    severity: str = "medium"
    confidence: str = "medium"

    def to_dict(self) -> dict[str, int | str]:
        payload: dict[str, int | str] = {
            "type": self.type,
            "message": self.message,
            "severity": self.severity,
            "confidence": self.confidence,
        }
        if self.line is not None:
            payload["line"] = self.line
        if self.column is not None:
            payload["column"] = self.column
        return payload


def clean_python_code(source: str, style_mode: str = "standard") -> tuple[str, list[dict[str, int | str]]]:
    issues: list[Issue] = []
    normalized = _normalize_source(source, issues)
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
                    confidence="high",
                )
            )
            candidate = _ensure_trailing_newline(candidate, issues)
            return candidate, [issue.to_dict() for issue in issues]

    parsed_tree, import_issues = _normalize_imports(parsed_tree)
    issues.extend(import_issues)
    parsed_tree, unused_import_issues = _remove_unused_imports(parsed_tree)
    issues.extend(unused_import_issues)
    parsed_tree, return_issues = _repair_missing_fallback_returns(parsed_tree)
    issues.extend(return_issues)
    issues.extend(_find_unreachable_code(parsed_tree))
    issues.extend(_find_inconsistent_returns(parsed_tree))

    if style_mode == "pep8":
        parsed_tree, pep8_issues = _apply_pep8_mode(parsed_tree)
        issues.extend(pep8_issues)

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
            line = _replace_leading_literal_tabs(line)

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
        issues.append(Issue("trailing_whitespace", "Removed trailing whitespace.", confidence="high"))
    if saw_tabs:
        issues.append(Issue("tabs", "Converted indentation tabs to spaces.", confidence="high"))
    if compressed_blank_lines:
        issues.append(Issue("blank_lines", "Compressed excessive blank lines.", confidence="high"))

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
    pass_repairs = False
    blank_run = 0
    in_triple_string = False
    triple_delimiter = ""

    for original_line in lines:
        toggled = _toggle_triple_quote_state(original_line, in_triple_string, triple_delimiter)
        if toggled:
            in_triple_string, triple_delimiter = toggled
            repaired_lines.append(original_line.rstrip())
            blank_run = 0 if original_line.strip() else blank_run + 1
            continue

        if original_line.strip() == "":
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

    repaired_lines, pass_repairs = _insert_missing_pass_blocks(repaired_lines)
    repaired_source = "\n".join(repaired_lines)
    repaired_source, imports_changed = _dedupe_plain_import_lines(repaired_source)
    if imports_changed:
        import_dedupe_repairs = True

    if indentation_repairs:
        issues.append(Issue("indentation", "Rebuilt indentation to produce parseable Python.", confidence="medium"))
    if colon_repairs:
        issues.append(Issue("missing_colons", "Inserted missing block colons where the syntax was obvious.", confidence="high"))
    if quote_repairs:
        issues.append(Issue("quotes", "Balanced simple mismatched quotes in broken string literals.", confidence="medium"))
    if compressed_blank_lines:
        issues.append(Issue("blank_lines", "Compressed excessive blank lines during repair.", confidence="high"))
    if import_dedupe_repairs:
        issues.append(Issue("imports", "Deduplicated repeated plain import statements during repair.", confidence="high"))
    if pass_repairs:
        issues.append(Issue("blocks", "Inserted pass statements into malformed empty blocks.", confidence="medium"))

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
        issues.append(Issue("imports", "Sorted and deduplicated top-level imports.", confidence="high"))

    new_body.extend(normalized_import_nodes)
    new_body.extend(body[index:])
    tree.body = new_body
    return tree, issues


def _remove_unused_imports(tree: ast.Module) -> tuple[ast.Module, list[Issue]]:
    used_names = _collect_used_names(tree)
    transformer = _UnusedImportStripper(used_names)
    updated = transformer.visit(tree)
    ast.fix_missing_locations(updated)
    issues = [
        Issue(
            "unused_imports",
            f"Removed unused import '{name}'.",
            line=line,
            severity="low",
            confidence="high",
        )
        for name, line in transformer.removed
    ]
    return updated, issues


def _repair_missing_fallback_returns(tree: ast.Module) -> tuple[ast.Module, list[Issue]]:
    issues: list[Issue] = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        fallback_name = _find_fallback_return_name(node)
        if not fallback_name:
            continue

        node.body.append(ast.Return(value=ast.Name(id=fallback_name, ctx=ast.Load())))
        issues.append(
            Issue(
                "returns",
                f"Added a fallback return for '{fallback_name}' to keep the function return path consistent.",
                line=node.lineno,
                confidence="medium",
            )
        )

    return tree, issues


def _find_unreachable_code(tree: ast.Module) -> list[Issue]:
    issues: list[Issue] = []
    for stmt_list in _iter_statement_lists(tree):
        terminated = False
        terminal_line: int | None = None
        for stmt in stmt_list:
            if terminated:
                issues.append(
                    Issue(
                        "unreachable_code",
                        "Found code after a terminal statement in the same block.",
                        line=getattr(stmt, "lineno", None),
                        severity="low",
                        confidence="high",
                    )
                )
                continue

            if isinstance(stmt, _TERMINAL_NODES):
                terminated = True
                terminal_line = getattr(stmt, "lineno", None)
            elif isinstance(stmt, ast.If):
                if _block_always_terminates(stmt.body) and _block_always_terminates(stmt.orelse):
                    terminated = True
                    terminal_line = getattr(stmt, "lineno", None)

        if terminated and terminal_line is not None:
            continue
    return issues


def _find_inconsistent_returns(tree: ast.Module) -> list[Issue]:
    issues: list[Issue] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        has_value_return = False
        has_bare_return = False
        for child in ast.walk(node):
            if isinstance(child, ast.Return):
                if child.value is None:
                    has_bare_return = True
                else:
                    has_value_return = True

        if has_value_return and not _function_always_returns(node):
            issues.append(
                Issue(
                    "return_paths",
                    "Function still has at least one path that can fall through without returning a value.",
                    line=node.lineno,
                    severity="medium",
                    confidence="low",
                )
            )
        elif has_value_return and has_bare_return:
            issues.append(
                Issue(
                    "return_paths",
                    "Function mixes value returns and bare returns.",
                    line=node.lineno,
                    severity="medium",
                    confidence="medium",
                )
            )
    return issues


def _apply_pep8_mode(tree: ast.Module) -> tuple[ast.Module, list[Issue]]:
    rename_map: dict[str, str] = {}
    issues: list[Issue] = []

    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        normalized_name = _to_snake_case(node.name)
        if normalized_name == node.name or normalized_name in rename_map.values():
            continue
        rename_map[node.name] = normalized_name

    if not rename_map:
        return tree, []

    transformer = _FunctionRenamer(rename_map)
    updated = transformer.visit(tree)
    ast.fix_missing_locations(updated)

    for old_name, new_name in rename_map.items():
        issues.append(
            Issue(
                "pep8_names",
                f"Renamed '{old_name}' to '{new_name}' in PEP8 mode.",
                severity="low",
                confidence="medium",
            )
        )

    return updated, issues


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
        Issue("quotes", "Normalized simple string quotes to single quotes.", confidence="high")
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


def _insert_missing_pass_blocks(lines: list[str]) -> tuple[list[str], bool]:
    if not lines:
        return lines, False

    changed = False
    updated: list[str] = []
    for index, line in enumerate(lines):
        updated.append(line)
        stripped = line.strip()
        if not stripped or not stripped.endswith(":") or not _BLOCK_HEADER_RE.match(stripped):
            continue

        current_indent = _leading_spaces(line)
        next_nonblank = _find_next_nonblank_line(lines, index + 1)
        if next_nonblank is None:
            updated.append(" " * (current_indent + 4) + "pass")
            changed = True
            continue

        next_indent = _leading_spaces(next_nonblank)
        if next_indent <= current_indent and not next_nonblank.strip().startswith(("#",)):
            updated.append(" " * (current_indent + 4) + "pass")
            changed = True

    return updated, changed


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
    issues.append(Issue("newline", "Ensured a final newline.", severity="low", confidence="high"))
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
    if not isinstance(last_stmt, ast.If) or last_stmt.orelse:
        return None

    returned_name = _single_returned_name(last_stmt.body)
    if not returned_name:
        return None

    assigned_before = _last_assigned_name_before_if(node.body[:-1])
    if assigned_before != returned_name:
        return None

    return returned_name


def _single_returned_name(statements: list[ast.stmt]) -> str | None:
    if not statements:
        return None

    last_stmt = statements[-1]
    if not isinstance(last_stmt, ast.Return) or not isinstance(last_stmt.value, ast.Name):
        return None

    for stmt in statements[:-1]:
        if isinstance(stmt, _TERMINAL_NODES):
            return None

    return last_stmt.value.id


def _last_assigned_name_before_if(statements: list[ast.stmt]) -> str | None:
    for stmt in reversed(statements):
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            return stmt.targets[0].id
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            return stmt.target.id
        if isinstance(stmt, (ast.Return, ast.Raise)):
            return None
    return None


def _collect_used_names(tree: ast.AST) -> set[str]:
    collector = _UsedNameCollector()
    collector.visit(tree)
    return collector.used_names


def _function_always_returns(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return _block_always_terminates(node.body)


def _block_always_terminates(statements: list[ast.stmt]) -> bool:
    if not statements:
        return False

    for stmt in statements:
        if isinstance(stmt, _TERMINAL_NODES):
            return True
        if isinstance(stmt, ast.If):
            if _block_always_terminates(stmt.body) and _block_always_terminates(stmt.orelse):
                return True
        if isinstance(stmt, (ast.For, ast.While, ast.With, ast.Try, ast.Match)):
            return False
    return False


def _iter_statement_lists(tree: ast.AST) -> list[list[ast.stmt]]:
    lists: list[list[ast.stmt]] = []
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            value = getattr(node, field, None)
            if isinstance(value, list) and value and all(isinstance(item, ast.stmt) for item in value):
                lists.append(value)
    return lists


def _to_snake_case(name: str) -> str:
    normalized = _SNAKE_CASE_EDGE_RE.sub("_", name).lower()
    normalized = re.sub(r"__+", "_", normalized)
    return normalized.strip("_") or name


def _leading_spaces(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _find_next_nonblank_line(lines: list[str], start: int) -> str | None:
    for line in lines[start:]:
        if line.strip():
            return line
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


class _UsedNameCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.used_names: set[str] = set()

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self.used_names.add(node.id)
        self.generic_visit(node)


class _UnusedImportStripper(ast.NodeTransformer):
    def __init__(self, used_names: set[str]) -> None:
        self.used_names = used_names
        self.removed: list[tuple[str, int | None]] = []

    def visit_Import(self, node: ast.Import):
        kept = []
        for alias in node.names:
            bound_name = alias.asname or alias.name.split(".")[0]
            if bound_name in self.used_names:
                kept.append(alias)
            else:
                self.removed.append((bound_name, getattr(node, "lineno", None)))
        if not kept:
            return None
        node.names = kept
        return node

    def visit_ImportFrom(self, node: ast.ImportFrom):
        if any(alias.name == "*" for alias in node.names):
            return node

        kept = []
        for alias in node.names:
            bound_name = alias.asname or alias.name
            if bound_name in self.used_names:
                kept.append(alias)
            else:
                self.removed.append((bound_name, getattr(node, "lineno", None)))
        if not kept:
            return None
        node.names = kept
        return node


class _FunctionRenamer(ast.NodeTransformer):
    def __init__(self, rename_map: dict[str, str]) -> None:
        self.rename_map = rename_map

    def visit_FunctionDef(self, node: ast.FunctionDef):
        node = self.generic_visit(node)
        node.name = self.rename_map.get(node.name, node.name)
        return node

    def visit_Call(self, node: ast.Call):
        node = self.generic_visit(node)
        if isinstance(node.func, ast.Name):
            node.func.id = self.rename_map.get(node.func.id, node.func.id)
        return node

    def visit_Name(self, node: ast.Name):
        if isinstance(node.ctx, ast.Load):
            node.id = self.rename_map.get(node.id, node.id)
        return node
