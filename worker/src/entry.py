from __future__ import annotations

import json
from urllib.parse import urlparse

from workers import Response, WorkerEntrypoint

from cleaner import clean_python_code
from runner import run_mock_test


JSON_HEADERS = {
    "content-type": "application/json; charset=utf-8",
    "access-control-allow-origin": "*",
    "access-control-allow-methods": "GET, POST, OPTIONS",
    "access-control-allow-headers": "content-type",
}


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        if request.method == "OPTIONS":
            return Response("", status=204, headers=JSON_HEADERS)

        path = urlparse(request.url).path.rstrip("/") or "/"
        if request.method == "GET":
            return _handle_get(path)

        if request.method != "POST":
            return _json_response(
                {
                    "cleaned_code": "",
                    "error": {
                        "type": "method_not_allowed",
                        "message": "Use POST with a JSON body containing a code field.",
                    },
                    "issues_found": [],
                },
                status=405,
            )

        if path not in {"/", "/audit"}:
            return _json_response(
                {
                    "cleaned_code": "",
                    "error": {
                        "type": "not_found",
                        "message": "Use POST /audit with a JSON body containing a code field.",
                    },
                    "issues_found": [],
                },
                status=404,
            )

        try:
            payload = await request.json()
        except Exception:
            return _json_response(
                {
                    "cleaned_code": "",
                    "error": {
                        "type": "invalid_json",
                        "message": "Request body must be valid JSON.",
                    },
                    "issues_found": [],
                },
                status=400,
            )

        code = _extract_code(payload)
        if not isinstance(code, str):
            return _json_response(
                {
                    "cleaned_code": "",
                    "error": {
                        "type": "invalid_request",
                        "message": "Request JSON must include a string field named code.",
                    },
                    "issues_found": [],
                },
                status=400,
            )

        cleaned_code, issues_found = clean_python_code(code)
        run_mock = _extract_run_mock(payload)
        mock_test = None

        if run_mock and not _has_unresolved_syntax_issue(issues_found):
            mock_test = run_mock_test(cleaned_code)

        return _json_response(
            {
                "cleaned_code": cleaned_code,
                "issues_found": issues_found,
                "mock_test": mock_test,
                "summary": (
                    "No issues detected."
                    if not issues_found
                    else f"{len(issues_found)} issue{'s' if len(issues_found) != 1 else ''} detected."
                ),
            }
        )


def _handle_get(path: str) -> Response:
    if path in {"/", "/health"}:
        return _json_response(
            {
                "name": "Python Code Checker API",
                "status": "ok",
                "audit_endpoint": "/audit",
            }
        )

    return _json_response(
        {
            "error": {
                "type": "not_found",
                "message": "Requested endpoint was not found.",
            }
        },
        status=404,
    )


def _extract_code(payload) -> str | None:
    if isinstance(payload, dict):
        value = payload.get("code")
        return value if isinstance(value, str) else None

    try:
        value = payload["code"]
    except Exception:
        return None

    return value if isinstance(value, str) else None


def _extract_run_mock(payload) -> bool:
    if not isinstance(payload, dict):
        return False
    return bool(payload.get("run_mock"))


def _has_unresolved_syntax_issue(issues_found: list[dict]) -> bool:
    for issue in issues_found:
        if issue.get("type") == "syntax_error":
            return True
    return False


def _json_response(payload: dict, status: int = 200) -> Response:
    return Response(json.dumps(payload, ensure_ascii=False), status=status, headers=JSON_HEADERS)
