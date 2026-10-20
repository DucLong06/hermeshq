"""Local auth endpoints for the auth router package.

Covers login, logout, refresh, me, password, avatar, forgot-password,
email-config and auth providers.
"""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hermeshq.config import get_settings
from hermeshq.core.security import (
    create_access_token,
    get_current_user,
    hash_password,
    require_admin,
    verify_password,
)
from hermeshq.database import get_db_session
from hermeshq.models.password_reset import PasswordResetToken
from hermeshq.models.user import User
from hermeshq.schemas.auth import (
    AuthProviderRead,
    AuthProvidersResponse,
    ChangePasswordRequest,
    EmailConfigStatus,
    ForgotPasswordRequest,
    LoginRequest,
    MfaRequiredResponse,
    PasswordResetResponse,
    ResetPasswordRequest,
    TokenResponse,
    UserPreferencesUpdate,
    UserProfileUpdate,
    UserRead,
)
from hermeshq.services.audit import record_audit
from hermeshq.services.avatar import (
    delete_avatar_files as _delete_avatar_files_shared,
)
from hermeshq.services.avatar import (
    resolve_media_type,
    validate_and_save_avatar,
)
from hermeshq.services.email_service import EmailServiceError, get_email_service

from .helpers import (
    _LOGIN_MAX_ATTEMPTS_PER_IP,
    _LOGIN_MAX_ATTEMPTS_PER_USER,
    _build_avatar_path,
    _check_login_rate,
    _clear_auth_cookie,
    _clear_login_attempts,
    _get_auth_mode,
    _record_login_attempt,
    _serialize_user,
    _set_auth_cookie,
    _user_avatar_base,
    _user_rate_key,
    logger,
)
from .mfa import _create_mfa_token, _is_mfa_globally_enabled, _mask_email, _send_mfa_code
from .oidc import (
    _get_oidc_provider_label,
    _get_oidc_provider_login_url,
    _get_public_oidc_provider_slugs,
    _oidc_enabled,
)

router = APIRouter()


@router.get("/providers", response_model=AuthProvidersResponse)
async def auth_providers(db: AsyncSession = Depends(get_db_session)) -> AuthProvidersResponse:
    auth_mode = _get_auth_mode()
    providers: list[AuthProviderRead] = []

    # Collect enabled DB providers
    db_provider_slugs: set[str] = set()
    try:
        from hermeshq.models.oidc_provider import OidcProvider

        result = await db.execute(select(OidcProvider).where(OidcProvider.enabled.is_(True)))
        for p in result.scalars().all():
            db_provider_slugs.add(p.slug)
            providers.append(
                AuthProviderRead(
                    slug=p.slug,
                    name=p.name,
                    kind="oidc",
                    enabled=True,
                )
            )
    except Exception:  # noqa: BLE001  # DB table may not exist on fresh install
        logger.debug("OIDC provider discovery failed; table may not exist yet", exc_info=True)

    # Add env-configured + always-visible providers (google, microsoft)
    for slug in _get_public_oidc_provider_slugs():
        if slug not in db_provider_slugs:
            env_url = _get_oidc_provider_login_url(slug)
            # Direct URL providers (google/microsoft) only need their explicit login URL —
            # full OIDC discovery config is not required for a direct redirect.
            enabled = bool(env_url)
            providers.append(
                AuthProviderRead(
                    slug=slug,
                    name=_get_oidc_provider_label(slug),
                    kind="oidc",
                    enabled=enabled,
                )
            )

    oidc_active = _oidc_enabled() or len(db_provider_slugs) > 0
    return AuthProvidersResponse(
        auth_mode=auth_mode,
        local_login_enabled=True,
        oidc_enabled=oidc_active,
        providers=providers,
    )


@router.post("/login")
async def login(
    payload: LoginRequest, response: Response, request: Request, db: AsyncSession = Depends(get_db_session)
):
    client_ip = request.client.host if request.client else "unknown"
    user_key = _user_rate_key(payload.username)
    _check_login_rate(client_ip, _LOGIN_MAX_ATTEMPTS_PER_IP)
    if payload.username:
        _check_login_rate(user_key, _LOGIN_MAX_ATTEMPTS_PER_USER)
    result = await db.execute(select(User).where(User.username == payload.username))
    user = result.scalar_one_or_none()
    if not user or not verify_password(payload.password, user.password_hash):
        _record_login_attempt(client_ip)
        if user is not None:
            _record_login_attempt(user_key)
        attempted = (payload.username or "")[:64]
        logger.warning("Failed login for %r from %s", attempted, client_ip)
        await record_audit(
            db,
            action="auth.login_failed",
            target_type="user",
            target_name=attempted,
            actor_username=attempted,
            ip_address=client_ip,
            details={"user_exists": user is not None},
        )
        await db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    _clear_login_attempts(user_key)

    # Check if MFA is required
    mfa_enabled = await _is_mfa_globally_enabled(db)
    if mfa_enabled and user.email:
        # Generate MFA token and send code
        mfa_token, mfa_expires = _create_mfa_token(user.id)
        try:
            await _send_mfa_code(db, user, client_ip)
        except EmailServiceError as exc:
            logger.warning("Failed to send MFA code to %s: %s", user.email, exc)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="MFA is enabled but email delivery is not configured. Contact your administrator.",
            )
        return MfaRequiredResponse(
            mfa_required=True,
            mfa_token=mfa_token,
            email_mask=_mask_email(user.email),
            expires_at=mfa_expires,
        )

    # No MFA — issue full token directly
    token, expires_at = create_access_token(user.id, subject_kind="id", role=user.role or "user")
    _set_auth_cookie(response, token)
    return TokenResponse(access_token=token, expires_at=expires_at)


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(
    response: Response,
    current_user: User = Depends(get_current_user),
) -> TokenResponse:
    """Issue a fresh JWT for the currently authenticated user.

    The client calls this before the existing token expires to extend
    the session without requiring a full re-login.
    """
    token, expires_at = create_access_token(current_user.id, subject_kind="id", role=current_user.role or "user")
    _set_auth_cookie(response, token)
    return TokenResponse(access_token=token, expires_at=expires_at)


# ---------------------------------------------------------------------------
# Password Reset (Resend email)
# ---------------------------------------------------------------------------


@router.post("/forgot-password", response_model=PasswordResetResponse)
async def forgot_password(
    payload: ForgotPasswordRequest,
    request: Request,
    db: AsyncSession = Depends(get_db_session),
) -> PasswordResetResponse:
    """Request a password reset link via email. Always returns 200 to prevent email enumeration."""
    # Find user by email (case-insensitive)
    result = await db.execute(
        select(User).where(
            func.lower(User.email) == payload.email.strip().lower(),
            User.is_active == True,  # noqa: E712
            User.auth_source == "local",
        )
    )
    user = result.scalar_one_or_none()

    if not user or not user.email:
        # Always return success to prevent enumeration
        return PasswordResetResponse(message="If that email is registered, a reset link has been sent.")

    # Rate limit: max 3 reset requests per user per hour
    one_hour_ago = datetime.now(UTC) - timedelta(hours=1)
    recent_result = await db.execute(
        select(func.count(PasswordResetToken.id)).where(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.created_at >= one_hour_ago,
        )
    )
    recent_count = recent_result.scalar() or 0
    if recent_count >= 3:
        return PasswordResetResponse(message="If that email is registered, a reset link has been sent.")

    # Generate a secure token
    raw_token = secrets.token_urlsafe(48)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    settings = get_settings()
    expires_minutes = settings.password_reset_token_minutes or 15
    expires_at = datetime.now(UTC) + timedelta(minutes=expires_minutes)

    # Get client IP
    ip = request.client.host if request.client else None

    reset_token = PasswordResetToken(
        user_id=user.id,
        token_hash=token_hash,
        expires_at=expires_at,
        ip_address=ip,
    )
    db.add(reset_token)
    await db.commit()

    # Send email
    email_service = get_email_service()
    await email_service.areload_config()
    try:
        await email_service.send_password_reset(
            to_email=user.email,
            token=raw_token,
            display_name=user.display_name,
        )
    except EmailServiceError as exc:
        logger.warning("Failed to send password reset email to %s: %s", user.email, exc)
        # Don't reveal the error to the client

    return PasswordResetResponse(message="If that email is registered, a reset link has been sent.")


@router.post("/reset-password", response_model=PasswordResetResponse)
async def reset_password(
    payload: ResetPasswordRequest,
    db: AsyncSession = Depends(get_db_session),
) -> PasswordResetResponse:
    """Reset password using a valid reset token."""
    # Hash the provided token to find it
    token_hash = hashlib.sha256(payload.token.encode()).hexdigest()

    result = await db.execute(
        select(PasswordResetToken).where(
            PasswordResetToken.token_hash == token_hash,
            PasswordResetToken.used_at.is_(None),
        )
    )
    reset_token = result.scalar_one_or_none()

    if not reset_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired reset token.",
        )

    # Check expiration
    now = datetime.now(UTC)
    if reset_token.expires_at.tzinfo is None:
        reset_token.expires_at = reset_token.expires_at.replace(tzinfo=UTC)
    if now > reset_token.expires_at:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Reset token has expired. Please request a new one.",
        )

    # Get user
    user = await db.get(User, reset_token.user_id)
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired reset token.",
        )

    # Update password
    user.password_hash = hash_password(payload.new_password)

    # Mark token as used
    reset_token.used_at = now

    # Invalidate any other pending reset tokens for this user
    other_result = await db.execute(
        select(PasswordResetToken).where(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.used_at.is_(None),
            PasswordResetToken.id != reset_token.id,
        )
    )
    for other_token in other_result.scalars().all():
        other_token.used_at = now

    await db.commit()

    logger.info("Password reset successful for user %s", user.username)

    return PasswordResetResponse(message="Password has been reset successfully.")


@router.get("/email-config", response_model=EmailConfigStatus)
async def get_email_config(
    _admin: User = Depends(require_admin),
) -> EmailConfigStatus:
    """Get current email configuration status (admin only)."""
    email_service = get_email_service()
    await email_service.areload_config()
    return EmailConfigStatus(
        configured=email_service.is_configured,
        from_email=email_service._from_email,
        from_name=email_service._from_name,
        public_base_url=email_service._public_base_url,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(response: Response, current_user: User = Depends(get_current_user)) -> Response:
    _clear_auth_cookie(response)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/me", response_model=UserRead)
async def me(request: Request, current_user: User = Depends(get_current_user)) -> UserRead:
    return _serialize_user(request, current_user)


@router.put("/me/preferences", response_model=UserRead)
async def update_preferences(
    payload: UserPreferencesUpdate,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> UserRead:
    if payload.theme_preference is not None:
        if payload.theme_preference not in {
            "default",
            "dark",
            "light",
            "system",
            "enterprise",
            "sixmanager",
            "sixmanager-light",
        }:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid theme preference")
        current_user.theme_preference = payload.theme_preference
    if payload.locale_preference is not None:
        if payload.locale_preference not in {"default", "en", "es"}:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid locale preference")
        current_user.locale_preference = payload.locale_preference
    await db.commit()
    await db.refresh(current_user)
    return _serialize_user(request, current_user)


@router.put("/me/profile", response_model=UserRead)
async def update_profile(
    payload: UserProfileUpdate,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> UserRead:
    current_user.display_name = payload.display_name
    await db.commit()
    await db.refresh(current_user)
    return _serialize_user(request, current_user)


@router.put("/me/password", status_code=status.HTTP_204_NO_CONTENT)
async def update_my_password(
    payload: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
):
    if not verify_password(payload.current_password, current_user.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Current password is incorrect")
    if payload.current_password == payload.new_password:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="New password must be different")
    current_user.password_hash = hash_password(payload.new_password)
    await db.commit()


@router.get("/me/avatar", include_in_schema=False)
async def get_my_avatar(current_user: User = Depends(get_current_user)):
    if not current_user.avatar_filename:
        raise HTTPException(status_code=404, detail="Avatar not found")
    avatar_path = _build_avatar_path(current_user)
    if not avatar_path or not avatar_path.exists():
        raise HTTPException(status_code=404, detail="Avatar not found")
    return FileResponse(avatar_path, media_type=resolve_media_type(avatar_path))


@router.post("/me/avatar", response_model=UserRead)
async def upload_my_avatar(
    request: Request,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> UserRead:
    current_user.avatar_filename = await validate_and_save_avatar(_user_avatar_base(), current_user.id, file)
    await db.commit()
    await db.refresh(current_user)
    return _serialize_user(request, current_user)


@router.delete("/me/avatar", response_model=UserRead)
async def delete_my_avatar(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> UserRead:
    _delete_avatar_files_shared(_user_avatar_base(), current_user.id)
    current_user.avatar_filename = None
    await db.commit()
    await db.refresh(current_user)
    return _serialize_user(request, current_user)
