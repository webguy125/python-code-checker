from __future__ import annotations

import io
import importlib
import inspect
import traceback
from contextlib import redirect_stdout


_SAFE_IMPORTS = {
    "collections",
    "datetime",
    "functools",
    "itertools",
    "json",
    "math",
    "os",
    "random",
    "re",
    "statistics",
    "string",
    "sys",
}


def run_mock_test(source: str) -> dict:
    buffer = io.StringIO()
    namespace = {"__builtins__": _safe_builtins(buffer)}

    try:
        compiled = compile(source, "<mock-test>", "exec")
        with redirect_stdout(buffer):
            exec(compiled, namespace, namespace)
            auto_called = _auto_call_zero_arg_functions(namespace)
    except Exception as exc:
        trace = traceback.extract_tb(exc.__traceback__)
        final_frame = trace[-1] if trace else None
        return {
            "status": "failed",
            "stdout": buffer.getvalue(),
            "error": {
                "type": exc.__class__.__name__,
                "message": str(exc) or "Mock test failed during execution.",
                "line": final_frame.lineno if final_frame else None,
            },
            "functions_discovered": _discover_functions(namespace, source),
            "functions_auto_called": [],
        }

    return {
        "status": "passed",
        "stdout": buffer.getvalue(),
        "error": None,
        "functions_discovered": _discover_functions(namespace, source),
        "functions_auto_called": auto_called,
    }


def _discover_functions(namespace: dict, source: str) -> list[str]:
    names: list[str] = []
    for key, value in namespace.items():
        if key.startswith("__"):
            continue
        if inspect.isfunction(value) and _is_user_defined(value, source):
            names.append(key)
    return sorted(names)


def _auto_call_zero_arg_functions(namespace: dict) -> list[str]:
    called: list[str] = []
    for name, value in sorted(namespace.items()):
        if name.startswith("__") or not inspect.isfunction(value):
            continue
        if not _is_zero_arg_callable(value):
            continue
        value()
        called.append(name)
    return called


def _blocked_import(*args, **kwargs):
    module_name = args[0] if args else ""
    root_name = module_name.split(".")[0]
    if root_name in _SAFE_IMPORTS:
        return importlib.import_module(module_name)
    raise RuntimeError(f"Import '{module_name}' is disabled during the mock test.")


def _blocked_input(*args, **kwargs):
    raise RuntimeError("Input is disabled during the mock test.")


def _safe_builtins(buffer: io.StringIO) -> dict:
    return {
        "__import__": _blocked_import,
        "abs": abs,
        "all": all,
        "any": any,
        "bool": bool,
        "dict": dict,
        "enumerate": enumerate,
        "float": float,
        "int": int,
        "len": len,
        "list": list,
        "max": max,
        "min": min,
        "print": lambda *args, **kwargs: print(*args, file=buffer, **kwargs),
        "range": range,
        "set": set,
        "sorted": sorted,
        "str": str,
        "sum": sum,
        "tuple": tuple,
        "zip": zip,
        "Exception": Exception,
        "ValueError": ValueError,
        "TypeError": TypeError,
        "input": _blocked_input,
    }


def _is_zero_arg_callable(func) -> bool:
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return False

    for parameter in signature.parameters.values():
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            continue
        if parameter.default is inspect._empty:
            return False
    return True


def _is_user_defined(func, source: str) -> bool:
    code = getattr(func, "__code__", None)
    if code is None:
        return False
    return code.co_filename == "<mock-test>"
