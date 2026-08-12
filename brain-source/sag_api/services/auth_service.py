"""Authentication and user domain logic (single user)."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sag_api.core.errors import AuthError, ConflictError, ForbiddenError, ValidationError
from sag_api.core.security import hash_password, verify_password
from sag_api.db.models import User


async def register_user(session: AsyncSession, *, email: str, password: str, name: str = "") -> User:
    from sag_api.core.config import settings

    existing = await session.scalar(select(User).where(User.email == email))
    if existing is not None:
        raise ConflictError("That email is already registered")

    # Personal use: the first registration is the only account; when registration is closed only the first user is let through (deployment bootstrap)
    user_count = await session.scalar(select(func.count()).select_from(User)) or 0
    if user_count > 0 and not settings.allow_registration:
        raise ForbiddenError("Registration is closed")

    user = User(
        email=email,
        password_hash=hash_password(password),
        name=name or email.split("@")[0],
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def authenticate(session: AsyncSession, *, email: str, password: str) -> User:
    user = await session.scalar(select(User).where(User.email == email))
    if user is None or not verify_password(password, user.password_hash):
        raise AuthError("Wrong email or password")
    if not user.is_active:
        raise ForbiddenError("The account is deactivated")
    return user


async def authenticate_or_register(
    session: AsyncSession,
    *,
    name: str = "",
    email: str = "",
    password: str | None = None,
) -> User:
    name = name.strip()
    email = email.strip().lower()
    password_supplied = bool(password)
    password = password or "admin"
    rename_local_user = False

    if email:
        user = await session.scalar(select(User).where(User.email == email))
    else:
        user = None
        if name:
            user = await session.scalar(
                select(User).where(User.name == name).order_by(User.created_at.asc()).limit(1)
            )
        if user is None:
            user = await session.scalar(
                select(User).order_by(User.created_at.asc()).limit(1)
            )
            rename_local_user = user is not None and bool(name) and user.name != name

    if user is not None:
        if password_supplied and not verify_password(password, user.password_hash):
            raise AuthError("Authentication failed")
        if not user.is_active:
            raise ForbiddenError("The account is deactivated")
        if rename_local_user:
            user.name = name
            await session.commit()
            await session.refresh(user)
        return user

    if not name:
        raise ValidationError("Please fill in your name first")

    user = User(
        email=email,
        password_hash=hash_password(password),
        name=name,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def get_user(session: AsyncSession, user_id: str) -> User | None:
    return await session.get(User, user_id)
