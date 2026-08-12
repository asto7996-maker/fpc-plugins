"""
Instant syntactic + safety analysis for self-heal patches.

Validates candidate Python source with ``ast.parse`` and rejects
obviously destructive constructs before a patch is kept on disk.
"""

from __future__ import annotations

import ast
import re
from typing import Optional, Tuple

# Substrings that look like destructive shell / filesystem ops.
_DANGEROUS_LITERAL_RE = re.compile(
    r"(?ix)"
    r"("
    r"rm\s+-rf\s+[/~.]"
    r"|rm\s+-rf\s+--"
    r"|:\(\)\s*\{\s*:\|:\s*&\s*\}\s*;?"
    r"|mkfs\."
    r"|dd\s+if="
    r"|:\s*>\s*/dev/sd"
    r"|shutil\.rmtree\s*\(\s*['\"]/"
    r"|os\.remove\s*\(\s*['\"]/"
    r")"
)

_DANGEROUS_CALL_NAMES = frozenset(
    {
        "system",
        "popen",
        "execl",
        "execle",
        "execlp",
        "execlpe",
        "execv",
        "execve",
        "execvp",
        "execvpe",
        "spawnl",
        "spawnle",
        "spawnlp",
        "spawnlpe",
        "spawnv",
        "spawnve",
        "spawnvp",
        "spawnvpe",
    }
)

_DANGEROUS_ATTR_OWNERS = frozenset({"os", "posix", "nt"})


def _attr_chain(node: ast.AST) -> list[str]:
    parts: list[str] = []
    cur: Optional[ast.AST] = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    return list(reversed(parts))


def _call_name(node: ast.Call) -> str:
    chain = _attr_chain(node.func)
    return ".".join(chain) if chain else ""


def _literal_strings(node: ast.AST) -> list[str]:
    out: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            out.append(child.value)
        elif isinstance(child, ast.JoinedStr):
            for v in child.values:
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    out.append(v.value)
    return out


def _is_dangerous_call(node: ast.Call) -> Optional[str]:
    name = _call_name(node)
    chain = _attr_chain(node.func)

    # os.system / os.popen / os.exec*
    if len(chain) >= 2 and chain[-2] in _DANGEROUS_ATTR_OWNERS:
        if chain[-1] in _DANGEROUS_CALL_NAMES:
            return f"запрещённый вызов `{name}`"

    # subprocess.* with shell=True + destructive literal
    if name.startswith("subprocess.") or (
        isinstance(node.func, ast.Name) and node.func.id in ("call", "run", "Popen", "check_call", "check_output")
    ):
        shell = False
        for kw in node.keywords:
            if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                shell = True
        literals = " ".join(_literal_strings(node))
        if shell and _DANGEROUS_LITERAL_RE.search(literals):
            return f"опасный subprocess shell-вызов: {literals[:80]!r}"
        if _DANGEROUS_LITERAL_RE.search(literals):
            return f"опасный аргумент subprocess: {literals[:80]!r}"

    # eval / exec of strings containing destructive patterns
    if isinstance(node.func, ast.Name) and node.func.id in ("eval", "exec", "compile"):
        literals = " ".join(_literal_strings(node))
        if _DANGEROUS_LITERAL_RE.search(literals):
            return f"опасный {node.func.id} с деструктивным литералом"

    # shutil.rmtree("/") etc.
    if name in ("shutil.rmtree", "os.remove", "os.unlink", "pathlib.Path.unlink"):
        for lit in _literal_strings(node):
            if lit.strip() in ("/", "~", "/*", "/workspace", "/root", "C:\\", "C:/"):
                return f"деструктивный путь в `{name}`: {lit!r}"
            if _DANGEROUS_LITERAL_RE.search(lit):
                return f"деструктивный литерал в `{name}`"

    # Direct string arg to os.system-like
    if name.endswith(".system") or name == "system":
        for lit in _literal_strings(node):
            if _DANGEROUS_LITERAL_RE.search(lit) or "rm -rf" in lit.lower():
                return f"опасный os.system: {lit[:120]!r}"
        # Any os.system in a heal patch is too risky for auto-apply
        return f"запрещённый вызов `{name}` в патче самолечения"

    return None


def _scan_dangerous(tree: ast.AST) -> Optional[str]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            reason = _is_dangerous_call(node)
            if reason:
                return reason
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _DANGEROUS_LITERAL_RE.search(node.value):
                return f"опасный строковый литерал: {node.value[:120]!r}"
    return None


def validate_python_code(code_string: str) -> Tuple[bool, Optional[str]]:
    """
    Validate Python source before applying a self-heal patch.

    Returns
    -------
    (True, None)
        Syntax is valid and no blocked constructs were found.
    (False, "описание ошибки AST")
        Parse failure or a dangerous construct was detected.
    """
    if code_string is None:
        return False, "пустой код (None)"
    if not isinstance(code_string, str):
        return False, f"ожидалась строка, получен {type(code_string).__name__}"

    try:
        tree = ast.parse(code_string)
    except SyntaxError as exc:
        loc = f"line {exc.lineno}" if exc.lineno else "unknown line"
        msg = exc.msg or "SyntaxError"
        return False, f"AST SyntaxError ({loc}): {msg}"
    except ValueError as exc:
        # e.g. null bytes
        return False, f"AST ValueError: {exc}"

    danger = _scan_dangerous(tree)
    if danger:
        return False, f"опасная конструкция: {danger}"

    return True, None
