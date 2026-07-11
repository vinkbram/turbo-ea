from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field, field_validator


def _validate_password_strength(v: str) -> str:
    if len(v) < 8:
        raise ValueError("Password must be at least 8 characters")
    if not any(c.isalpha() for c in v):
        raise ValueError("Password must contain at least one letter")
    if not any(c.isdigit() for c in v):
        raise ValueError("Password must contain at least one digit")
    return v


class RegisterRequest(BaseModel):
    email: EmailStr
    display_name: str = Field(..., min_length=1, max_length=200)
    password: str

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        return _validate_password_strength(v)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserResponse(BaseModel):
    id: str
    email: str
    display_name: str
    role: str
    role_label: str | None = None
    role_color: str | None = None
    is_active: bool
    locale: str = "en"
    permissions: dict[str, bool] | None = None
    ui_preferences: dict | None = None
    # Set when the JWT carries an ``impersonated_role`` claim — the
    # frontend uses this to render the persistent "viewing as X" banner.
    # The ``role`` / ``role_label`` / ``role_color`` / ``permissions``
    # fields above already reflect the impersonated role so the rest of
    # the UI behaves identically to a fresh login as that role.
    impersonated_role: str | None = None
    impersonated_role_label: str | None = None

    model_config = {"from_attributes": True}


class ImpersonateRequest(BaseModel):
    role: str = Field(..., min_length=1, max_length=64)
