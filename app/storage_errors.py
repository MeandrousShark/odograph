"""Present protected storage refusals after the transaction has rolled back."""
from fastapi import Request
from psycopg import errors
from starlette.responses import JSONResponse

from app.storage import is_storage_capacity_error


def register_storage_errors(app):
    @app.exception_handler(errors.RaiseException)
    async def storage_refusal(request: Request, exc: errors.RaiseException):
        if not is_storage_capacity_error(exc):
            raise exc
        return JSONResponse(
            {"detail": "Storage is full. This change wasn't saved. Your existing data is still available. "
                       "Export a copy before removing data, or ask the operator for more space."},
            status_code=503,
            headers={"Retry-After": "60"},
        )
