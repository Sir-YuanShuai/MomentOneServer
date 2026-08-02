from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.errors import ApplicationError
from app.core.request_context import request_id_context


async def application_error_handler(_request: Request, error: Exception) -> JSONResponse:
    if not isinstance(error, ApplicationError):
        raise error

    return JSONResponse(
        status_code=error.status_code,
        content={
            "error": {
                "code": error.code,
                "message": error.message,
                "requestId": request_id_context.get(),
                "details": error.details,
            }
        },
    )


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ApplicationError, application_error_handler)
