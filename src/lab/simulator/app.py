"""HTTP layer for the payment simulator.

Two apps, two ports, two tokens. The public app is what agent tooling calls. The
internal app configures hidden faults and barriers and is never registered as a tool.
Neither app publishes an OpenAPI schema.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Callable
from typing import Any

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from psycopg_pool import ConnectionPool, PoolTimeout
from pydantic import BaseModel, ConfigDict, Field

from lab.protocol.faults import FaultPlan
from lab.protocol.task import Profile
from lab.simulator import service

logger = logging.getLogger(__name__)


class NamespaceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: Profile
    fault_plan: FaultPlan = Field(default_factory=FaultPlan)


def _bearer(expected: str) -> Callable[[str], None]:
    def check(authorization: str = Header(default="")) -> None:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(
            token.encode(), expected.encode()
        ):
            raise HTTPException(status_code=401, detail="unauthorized")

    return check


def _json(resp: service.SimResponse) -> JSONResponse:
    return JSONResponse(resp.body, status_code=resp.http_status)


def _unknown_namespace() -> JSONResponse:
    body = {"error": {"code": "unknown_namespace", "message": "unknown namespace"}}
    return JSONResponse(body, status_code=404)


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status_code)


async def _pool_timeout(request: Request, exc: Exception) -> JSONResponse:
    # Known limitation: a PoolTimeout means no database connection could be acquired, so
    # there is no connection to write a call_log row with, and this layer deliberately
    # does not open a second one or use a side channel. The oracle therefore undercounts
    # an agent call that died here. That is recorded as a limitation, not worked around.
    logger.error("connection pool timeout on %s %s", request.method, request.url.path, exc_info=exc)
    return _error_response(503, "service_unavailable", "service temporarily unavailable")


async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
    # Same call_log limitation as above whenever the failure precedes the claim.
    logger.error("unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
    return _error_response(500, "unhandled_error", "unhandled server error")


def _bare_app(title: str) -> FastAPI:
    app = FastAPI(title=title, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_exception_handler(PoolTimeout, _pool_timeout)
    app.add_exception_handler(Exception, _unhandled)
    return app


def create_public_app(pool: ConnectionPool, client_token: str) -> FastAPI:
    app = _bare_app("payment-simulator")
    auth = [Depends(_bearer(client_token))]

    @app.post("/v1/charges", dependencies=auth)
    def post_charge(payload: dict[str, Any], x_lab_namespace: str = Header()) -> JSONResponse:
        with pool.connection() as conn:
            try:
                return _json(service.charge(conn, x_lab_namespace, payload))
            except service.UnknownNamespace:
                return _unknown_namespace()

    @app.get("/v1/payments/{idempotency_key}", dependencies=auth)
    def get_payment(idempotency_key: str, x_lab_namespace: str = Header()) -> JSONResponse:
        with pool.connection() as conn:
            try:
                return _json(service.status(conn, x_lab_namespace, idempotency_key))
            except service.UnknownNamespace:
                return _unknown_namespace()

    return app


def create_internal_app(pool: ConnectionPool, control_token: str) -> FastAPI:
    app = _bare_app("payment-simulator-control")
    auth = [Depends(_bearer(control_token))]

    @app.put("/internal/namespaces/{namespace}", dependencies=auth)
    def put_namespace(namespace: str, config: NamespaceConfig) -> dict[str, str]:
        with pool.connection() as conn:
            service.configure_namespace(conn, namespace, config.profile, config.fault_plan)
        return {"namespace": namespace}

    @app.delete("/internal/namespaces/{namespace}", dependencies=auth)
    def delete_namespace(namespace: str) -> Response:
        with pool.connection() as conn:
            service.delete_namespace(conn, namespace)
        return Response(status_code=204)

    @app.get("/internal/namespaces/{namespace}/barriers/{name}", dependencies=auth)
    def get_barrier(namespace: str, name: str) -> dict[str, bool]:
        with pool.connection() as conn:
            return service.barrier_state(conn, namespace, name)

    @app.post("/internal/namespaces/{namespace}/barriers/{name}/release", dependencies=auth)
    def release(namespace: str, name: str) -> Any:
        with pool.connection() as conn:
            try:
                service.release_barrier(conn, namespace, name)
            except psycopg.errors.ForeignKeyViolation:
                return _unknown_namespace()
        return {"released": True}

    return app
