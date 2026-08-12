"""FastAPI dependencies: authentication + application-level singletons. Single user, no workspaces or roles."""

from __future__ import annotations

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from sag_agent import AgentRuntime
from sag_api.core.db import get_session
from sag_api.core.errors import AuthError
from sag_api.core.security import decode_token
from sag_api.db.models import User
from sag_api.generation import LLMClient
from sag_api.jobs import JobQueue
from sag_api.sag import EngineManager
from sag_api.services.auth_service import get_user

_bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_session),
) -> User:
    if creds is None:
        raise AuthError("Missing authentication token")
    try:
        payload = decode_token(creds.credentials)
    except jwt.PyJWTError as e:
        raise AuthError("Token is invalid or expired") from e
    user_id = payload.get("sub")
    user = await get_user(session, user_id) if user_id else None
    if user is None or not user.is_active:
        raise AuthError("User does not exist or is deactivated")
    request.state.user = user
    return user


def get_engine_manager(request: Request) -> EngineManager:
    return request.app.state.engine_manager


def get_job_queue(request: Request) -> JobQueue:
    return request.app.state.job_queue


def get_llm(request: Request) -> LLMClient:
    return request.app.state.llm


def get_agent_runtime(request: Request) -> AgentRuntime:
    return request.app.state.agent_runtime


def get_tool_registry():
    """Agent tool registry (built-in retrieval/entity tools + MCP tools injected at runtime)."""
    from sag_api.tools import registry

    return registry
