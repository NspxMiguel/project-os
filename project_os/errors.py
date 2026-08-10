"""One error contract for the whole API.

Every failure -- raised by us, by FastAPI validation, by Starlette, or by a bug
we did not anticipate -- leaves the process as::

    {"error": "<code>", "message": "<human readable>", "detail": <any|null>}

so the frontend has exactly one shape to parse.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger(__name__)

#: Human-friendly codes for the status codes we actually use.
STATUS_CODES = {
    400: "bad_request",
    401: "unauthenticated",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    422: "validation_error",
    428: "setup_required",
    429: "rate_limited",
    500: "internal_error",
    501: "not_implemented",
    503: "unavailable",
}


def code_for_status(status: int) -> str:
    return STATUS_CODES.get(status, "http_%d" % status)


class ApiError(Exception):
    """An error with a stable machine-readable code."""

    def __init__(
        self,
        status: int = 400,
        code: str = "",
        message: str = "",
        detail: Any = None,
    ) -> None:
        self.status = int(status)
        self.code = code or code_for_status(self.status)
        self.message = message or self.code.replace("_", " ")
        self.detail = detail
        super().__init__("%s: %s" % (self.code, self.message))

    def to_dict(self) -> Dict[str, Any]:
        return {"error": self.code, "message": self.message, "detail": self.detail}

    def response(self) -> JSONResponse:
        return JSONResponse(status_code=self.status, content=self.to_dict())


def bad_request(message: str, code: str = "bad_request", detail: Any = None) -> ApiError:
    return ApiError(400, code, message, detail)


def unauthenticated(message: str = "Entre na sua conta para continuar.", detail: Any = None) -> ApiError:
    return ApiError(401, "unauthenticated", message, detail)


def forbidden(message: str, code: str = "forbidden", detail: Any = None) -> ApiError:
    return ApiError(403, code, message, detail)


def not_found(message: str, code: str = "not_found", detail: Any = None) -> ApiError:
    return ApiError(404, code, message, detail)


def conflict(message: str, code: str = "conflict", detail: Any = None) -> ApiError:
    return ApiError(409, code, message, detail)


def setup_required(
    message: str = "O project-os ainda não tem usuário. Termine a configuração primeiro.",
) -> ApiError:
    return ApiError(428, "setup_required", message)


def unavailable(message: str, code: str = "unavailable", detail: Any = None) -> ApiError:
    return ApiError(503, code, message, detail)


def _jsonable(value: Any) -> Any:
    """Best-effort coercion so a detail payload can never break the response."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return dict((str(k), _jsonable(v)) for k, v in value.items())
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return str(value)


async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    if exc.status >= 500:
        log.error("%s %s -> %s: %s", request.method, request.url.path, exc.code, exc.message)
    return JSONResponse(
        status_code=exc.status,
        content={
            "error": exc.code,
            "message": exc.message,
            "detail": _jsonable(exc.detail),
        },
    )


async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    errors = _jsonable(exc.errors())
    return JSONResponse(
        status_code=422,
        content={
            "error": "validation_error",
            "message": "O que foi enviado não está válido.",
            "detail": errors,
        },
    )


async def http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    detail = exc.detail
    # Allow other modules to raise HTTPException(status, {"error": ..., ...})
    # and still land on the one contract.
    if isinstance(detail, dict) and "error" in detail:
        body = {
            "error": str(detail.get("error")),
            "message": str(detail.get("message") or detail.get("error")),
            "detail": _jsonable(detail.get("detail")),
        }
    else:
        message = detail if isinstance(detail, str) and detail else code_for_status(exc.status_code)
        body = {
            "error": code_for_status(exc.status_code),
            "message": str(message).replace("_", " "),
            "detail": None if isinstance(detail, str) else _jsonable(detail),
        }
    headers = getattr(exc, "headers", None)
    return JSONResponse(status_code=exc.status_code, content=body, headers=headers)


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "error": "internal_error",
            "message": "Deu problema aqui no project-os.",
            "detail": {"type": type(exc).__name__},
        },
    )


def install_error_handlers(app: FastAPI) -> None:
    """Register every handler on a FastAPI application."""
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)


__all__ = [
    "ApiError",
    "STATUS_CODES",
    "code_for_status",
    "bad_request",
    "unauthenticated",
    "forbidden",
    "not_found",
    "conflict",
    "setup_required",
    "unavailable",
    "install_error_handlers",
    "api_error_handler",
    "validation_error_handler",
    "http_exception_handler",
    "unhandled_exception_handler",
]
