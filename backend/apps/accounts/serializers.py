"""Request/response shapes for the auth endpoints (T0.6) and the user-account
management endpoints (`GET/POST /api/v1/users` — see `apps.accounts.services`
module docstring for why the latter exists)."""

from __future__ import annotations

from rest_framework import serializers

from .models import User


class LoginSerializer(serializers.Serializer):
    """`POST /api/v1/auth/login` payload.

    ASSUMPTION (flagged for frontend-engineer/code-reviewer):
    `docs/api-and-ui.md` documents the login endpoint's *path* but not its
    request body shape. Because `apps.accounts.models.User.email` is unique
    only **per tenant** (docs/data-model.md), the tenant must be disambiguated
    from the client before any user lookup is possible — there is no session
    yet to derive it from (that's the whole point of login). This adds a
    required `tenant` **slug** field to the payload for that purpose only;
    see `apps.accounts.api.LoginView` for why this is the one legitimate,
    reviewed exception to "the tenant is inferred from the session, never
    the client" (R4): it is used solely to select which tenant's `(tenant,
    email)` row to check the password against, and never sets any session/
    request tenant context until the password has been verified.
    """

    tenant = serializers.SlugField()
    email = serializers.EmailField()
    password = serializers.CharField(trim_whitespace=False, write_only=True)


class UserSerializer(serializers.ModelSerializer):
    """Read shape for `GET /api/v1/users` and the create-response's user
    fields — id/email/name only, deliberately excludes `password`/any other
    sensitive field."""

    class Meta:
        model = User
        fields = ["id", "email", "name"]
        read_only_fields = fields


class CreateUserSerializer(serializers.Serializer):
    """`POST /api/v1/users` request payload.

    Deliberately has NO `password` field — this endpoint always generates
    one server-side (see `apps.accounts.services.
    create_user_with_generated_password`); accepting a client-supplied
    password here would reopen the weak-password-creation door the task
    exists to close.
    """

    email = serializers.EmailField()
    name = serializers.CharField(max_length=255, allow_blank=True, default="")

    def validate_email(self, value: str) -> str:
        # Same "no bare IntegrityError" pattern `apps.catalog.serializers`/
        # `apps.rbac.serializers.MembershipSerializer.validate` already use
        # for a uniqueness constraint (`uniq_user_tenant_email`) —
        # `User.objects` is the tenant-scoped, fail-closed manager, so this
        # checks uniqueness WITHIN the current tenant only, never globally
        # (R4: the same email may legitimately exist in a different tenant).
        email = User.all_objects.normalize_email(value)
        if User.objects.filter(email=email).exists():
            raise serializers.ValidationError(
                "A user with this email already exists in this tenant."
            )
        return email


class UpdateMeSerializer(serializers.Serializer):
    """`PATCH /api/v1/me` — the signed-in user edits their OWN profile.

    Deliberately a one-field allowlist (`name`): `email` is the login
    identifier (changing it is an admin/`user.manage` concern, not
    self-service), and nothing else on `User` — `is_active`, `is_staff`,
    `is_superuser`, `tenant` — may EVER be self-settable. An explicit
    `Serializer` (not a `ModelSerializer`) is what makes that allowlist
    unmissable at review time.
    """

    name = serializers.CharField(max_length=255, allow_blank=True, trim_whitespace=True)


class ChangePasswordSerializer(serializers.Serializer):
    """`POST /api/v1/me/password` — self-service password change payload.

    Only shape validation here (both fields required, non-empty). The
    `current_password` check (`user.check_password`) and the strength check on
    `new_password` (`AUTH_PASSWORD_VALIDATORS`, needs the `User` for the
    similarity validator) both happen in the view/service where the user is in
    hand — see `apps.accounts.services.set_user_password`.
    """

    current_password = serializers.CharField(trim_whitespace=False, write_only=True)
    new_password = serializers.CharField(trim_whitespace=False, write_only=True)


class ForgotPasswordRequestSerializer(serializers.Serializer):
    """`POST /api/v1/auth/password-reset/request` payload. `tenant` slug
    disambiguates the per-tenant-unique email, exactly as `LoginSerializer`
    does (see its docstring for the reviewed R4 exception)."""

    tenant = serializers.SlugField()
    email = serializers.EmailField()


class PasswordResetConfirmSerializer(serializers.Serializer):
    """`POST /api/v1/auth/password-reset/confirm` payload. `tenant` slug lets
    the unauthenticated confirm endpoint enter the token's tenant context
    before looking it up by hash (RLS needs a tenant — same path as login)."""

    tenant = serializers.SlugField()
    token = serializers.CharField(trim_whitespace=False, write_only=True)
    new_password = serializers.CharField(trim_whitespace=False, write_only=True)
