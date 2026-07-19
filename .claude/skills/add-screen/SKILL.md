---
name: add-screen
description: >-
  The procedure for adding a React + TypeScript PWA screen or major component to the
  LMS frontend so it is mobile-first, uses the typed API client against the fixed
  contract, gates actions by the user's effective permissions, and handles errors
  and long lists correctly. Use for any new page/screen or dynamic form. Trigger
  cues: "build the X screen", "new page/route", "add a form", "dynamic custom-field
  form", "list/table view", "calendar UI".
---

# Adding an LMS PWA screen

Mobile-first, one-handed, server-driven. Read `docs/api-and-ui.md` (the screen list +
the API contract — it's fixed) and `docs/features.md` (acceptance) first.

## Checklist

1. **Find the screen + its endpoints** in `docs/api-and-ui.md`. Build only against the
   documented endpoints. If a needed endpoint is missing, stop and flag it for
   backend-engineer rather than inventing a response shape.

2. **Use the typed API client.** All calls go through the single typed client module
   (base `/api/v1`, same-origin via nginx). Never build URLs ad hoc in a component.
   Include the CSRF token on writes. Handle RFC-7807 problem+json errors uniformly.

3. **Mobile-first & one-handed.** Assume a phone. Use Mantine primitives (they bring
   accessibility + dark mode — don't fight them). The Scan FAB is the primary action on
   Home; keep primary actions thumb-reachable.

4. **Server-side for lists.** Never load "all" of anything. Use the paginated endpoints
   with `?search=`, `?ordering=`, and filters; **virtualize** long lists. No client-side
   full-dataset filtering.

5. **Gate actions by permissions.** Read effective permissions from `GET /me` and hide
   actions the user can't perform — but treat a server **403 as a normal handled
   outcome**, never assume the client gate is the security boundary. Actions belonging
   to a later milestone render as present-but-disabled stubs.

6. **Category-driven dynamic forms** (asset create/edit): render fields from the
   selected category's `CustomFieldDef` list, respecting `data_type`
   (text|int|float|bool|date|enum|json), `required`, `unit`, `enum_options`, and
   `order`. Changing the category changes the field set.

7. **States.** Handle loading, empty, and error states explicitly. Show conflict/
   validation feedback inline (e.g. reservation overlap 409).

8. **Verify.** Run `tsc` and the build; exercise the screen against the real backend
   where possible. Note that camera/scan/photo screens need the HTTPS secure context
   (that's pwa-scan-specialist territory — coordinate rather than reimplement).

## Definition of done
The screen matches the contract and its acceptance criterion, is usable one-handed on a
phone, drives lists server-side, gates actions by `/me`, handles error/empty/loading,
and passes `tsc` + build. Then it's ready for **code-reviewer**.
