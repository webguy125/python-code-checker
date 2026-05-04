from __future__ import annotations

import builtins
import inspect
import io
import importlib
import traceback
from contextlib import redirect_stdout
from dataclasses import dataclass


_SAFE_IMPORTS = {
    "collections",
    "datetime",
    "functools",
    "itertools",
    "json",
    "math",
    "random",
    "re",
    "statistics",
    "string",
}


@dataclass(frozen=True)
class SampleCall:
    name: str
    args: list[str]
    status: str
    result: str | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "args": self.args,
            "status": self.status,
            "result": self.result,
            "error": self.error,
        }


def run_mock_test(source: str) -> dict:
    buffer = io.StringIO()
    namespace = {
        "__builtins__": _safe_builtins(buffer),
        "__name__": "__mock_test__",
    }

    try:
        compiled = compile(source, "<mock-test>", "exec")
        with redirect_stdout(buffer):
            exec(compiled, namespace, namespace)
            auto_called = _auto_call_zero_arg_functions(namespace)
            sample_calls = _sample_call_functions(namespace)
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
            "functions_auto_called": [],
            "sample_calls": [],
        }

    return {
        "status": "passed",
        "stdout": buffer.getvalue(),
        "error": None,
        "functions_discovered": _discover_functions(namespace),
        "functions_auto_called": auto_called,
        "sample_calls": [call.to_dict() for call in sample_calls],
    }


def _discover_functions(namespace: dict) -> list[str]:
    names: list[str] = []
    for key, value in namespace.items():
        if key.startswith("__"):
            continue
        if inspect.isfunction(value) and _is_user_defined(value):
            names.append(key)
    return sorted(names)


def _auto_call_zero_arg_functions(namespace: dict) -> list[str]:
    called: list[str] = []
    for name, value in sorted(namespace.items()):
        if name.startswith("__") or not inspect.isfunction(value):
            continue
        if inspect.iscoroutinefunction(value):
            continue
        if not _is_zero_arg_callable(value):
            continue
        value()
        called.append(name)
    return called


def _sample_call_functions(namespace: dict) -> list[SampleCall]:
    reports: list[SampleCall] = []
    for name, value in sorted(namespace.items()):
        if name.startswith("__") or not inspect.isfunction(value):
            continue
        if inspect.iscoroutinefunction(value):
            continue
        if not _is_user_defined(value) or _is_zero_arg_callable(value):
            continue

        generated = _generate_sample_args(value)
        if generated is None:
            continue

        args, rendered = generated
        try:
            result = value(*args)
            reports.append(
                SampleCall(
                    name=name,
                    args=rendered,
                    status="passed",
                    result=repr(result),
                )
            )
        except Exception as exc:
            reports.append(
                SampleCall(
                    name=name,
                    args=rendered,
                    status="failed",
                    error=f"{exc.__class__.__name__}: {exc}",
                )
            )
    return reports


def _generate_sample_args(func) -> tuple[list, list[str]] | None:
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return None

    args: list = []
    rendered: list[str] = []
    for parameter in signature.parameters.values():
        if parameter.kind in (parameter.VAR_KEYWORD, parameter.VAR_POSITIONAL):
            continue
        if parameter.default is not inspect._empty:
            args.append(parameter.default)
            rendered.append(repr(parameter.default))
            continue

        sample = _sample_value_for_parameter(parameter.name)
        args.append(sample)
        rendered.append(repr(sample))

    return args, rendered


def _sample_value_for_parameter(name: str):
    lowered = name.lower()
    if any(token in lowered for token in ("count", "size", "total", "amount", "radius", "width", "height", "index")):
        return 4
    if any(token in lowered for token in ("text", "name", "label", "title", "message")):
        return "sample"
    if any(token in lowered for token in ("items", "values", "rows")):
        return [1, 2, 3]
    if any(token in lowered for token in ("flag", "enabled", "is_")):
        return True
    if lowered in {"x", "y", "a", "b"}:
        return 3 if lowered in {"x", "a"} else 2
    return 1


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
        "__build_class__": builtins.__build_class__,
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
        "object": object,
        "zip": zip,
        "Exception": Exception,
        "RuntimeError": RuntimeError,
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


def _is_user_defined(func) -> bool:
    code = getattr(func, "__code__", None)
    if code is None:
        return False
    return code.co_filename == "<mock-test>"
