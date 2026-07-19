"""Request/response shapes for the T0.6 auth endpoints."""

from __future__ import annotations

from rest_framework import serializers


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
