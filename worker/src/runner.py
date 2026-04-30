from __future__ import annotations

import io
import importlib
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
            "functions_discovered": _discover_functions(namespace),
        }

    return {
        "status": "passed",
        "stdout": buffer.getvalue(),
        "error": None,
        "functions_discovered": _discover_functions(namespace),
    }


def _discover_functions(namespace: dict) -> list[str]:
    names: list[str] = []
    for key, value in namespace.items():
        if key.startswith("__"):
            continue
        if callable(value):
            names.append(key)
    return sorted(names)


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
