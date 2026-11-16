"""The SIMULATED labels of the synthetic app (Option B B3b, R-B3b-5 / B-L1). SYNTHETIC ONLY.

Every response carries the header `x-haloflow-simulated: true` (added by the gate) and every
JSON body is an object holding `"simulated": true` (added here). The wrapper adds the key to
production's own error bodies and never removes or renames a key. A payload that already holds
`simulated` is a defect: `labelled` raises, and the caller answers 500 INTERNAL.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Final

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

LABEL_HEADER: Final = "x-haloflow-simulated"
LABEL_KEY: Final = "simulated"
INTERNAL: Final = {"code": "INTERNAL"}


class LabelConflict(Exception):
    """A payload already holding the label key (a defect, answered 500 INTERNAL)."""


def labelled(content: Mapping[str, Any], status_code: int = 200,
             headers: Mapping[str, str] | None = None) -> JSONResponse:
    """A JSON response whose object body holds `"simulated": true`. Raises LabelConflict for a
    payload that already has the key, TypeError/ValueError for a non-encodable payload."""
    body = dict(content)
    if LABEL_KEY in body:
        raise LabelConflict("payload already labelled")
    body[LABEL_KEY] = True
    return JSONResponse(body, status_code=status_code, headers=dict(headers or {}))


def relabel(response: Response) -> Response:
    """A production JSON response (fixed body) with the label added; anything else is 500."""
    try:
        content = json.loads(bytes(response.body))
        if type(content) is not dict:
            raise ValueError("not an object")
        return labelled(content, response.status_code)
    except Exception:  # noqa: BLE001 - a malformed fixed body is a defect
        return internal()


def internal() -> JSONResponse:
    return JSONResponse({**INTERNAL, LABEL_KEY: True}, status_code=500)


def refusal(status_code: int, code: str, **extra: str) -> JSONResponse:
    return labelled({"code": code, **extra}, status_code)


async def not_found(_request: Request, _exc: Exception) -> Response:
    return refusal(404, "NOT_FOUND")


async def method_not_allowed(_request: Request, _exc: Exception) -> Response:
    return refusal(405, "METHOD_NOT_ALLOWED")


EXCEPTION_HANDLERS: Final[dict[int | type[Exception], Any]] = {404: not_found,
                                                               405: method_not_allowed}
