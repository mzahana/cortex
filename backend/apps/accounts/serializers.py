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
