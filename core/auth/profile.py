"""Profile editing and password change.

Split out of ``core.auth.users`` so that file stays close to its line budget.
The functions here mutate user rows directly and raise ``ValueError`` on
validation failures — the HTTP layer maps those to HTTP 400.
"""

from __future__ import annotations

import re
import secrets
import sqlite3

from .schema import _conn, _hash_password


_PHONE_RE = re.compile(r"^\+?[0-9 ()\-]{6,32}$")


def _normalize_optional(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _row_to_user_dict(row) -> dict:
    user = dict(row)
    user.pop("password_hash", None)
    user.pop("salt", None)
    return user


def update_profile(
    user_id: str,
    *,
    username: str | None = None,
    full_name: str | None = None,
    phone: str | None = None,
    email: str | None = None,
) -> dict:
    """Update mutable profile fields on the ``users`` row.

    Pass ``None`` to leave a field untouched, ``""`` to clear ``full_name`` /
    ``phone``. Username and email cannot be cleared. Email is normalised to
    lower-case and checked for uniqueness; raises ``ValueError`` on collision
    or invalid input.
    """
    updates: list[tuple[str, object]] = []

    if username is not None:
        normalized_username = str(username).strip()
        if len(normalized_username) < 3 or len(normalized_username) > 32:
            raise ValueError("Username must be between 3 and 32 characters.")
        updates.append(("username", normalized_username))

    if email is not None:
        normalized_email = str(email).strip().lower()
        if not normalized_email or "@" not in normalized_email:
            raise ValueError("Enter a valid email address.")
        updates.append(("email", normalized_email))

    if full_name is not None:
        normalized_full_name = _normalize_optional(full_name)
        if normalized_full_name is not None and len(normalized_full_name) > 80:
            raise ValueError("Full name must be 80 characters or fewer.")
        updates.append(("full_name", normalized_full_name))

    if phone is not None:
        normalized_phone = _normalize_optional(phone)
        if normalized_phone is not None and not _PHONE_RE.match(normalized_phone):
            raise ValueError("Enter a valid phone number.")
        updates.append(("phone", normalized_phone))

    if not updates:
        with _conn() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
        if row is None:
            raise ValueError("Account not found.")
        return _row_to_user_dict(row)

    set_clause = ", ".join(f"{col} = ?" for col, _ in updates)
    params = [val for _, val in updates] + [user_id]
    with _conn() as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                f"UPDATE users SET {set_clause} WHERE user_id = ?", params
            )
            row = conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
        except sqlite3.IntegrityError as exc:
            message = str(exc).lower()
            if "users.email" in message or "unique constraint failed: users.email" in message:
                raise ValueError("An account with that email already exists.")
            raise

    if row is None:
        raise ValueError("Account not found.")
    return _row_to_user_dict(row)


def change_password(user_id: str, current_password: str, new_password: str) -> None:
    """Verify ``current_password`` and replace it with ``new_password``.

    Raises ``ValueError`` with a user-facing message on any failure.
    """
    if not isinstance(current_password, str) or not current_password:
        raise ValueError("Current password is required.")
    if not isinstance(new_password, str):
        raise ValueError("New password is required.")
    if (
        len(new_password) < 8
        or not any(c.isalpha() for c in new_password)
        or not any(c.isdigit() for c in new_password)
    ):
        raise ValueError("Password must be at least 8 characters and include letters and numbers.")

    with _conn() as conn:
        row = conn.execute(
            "SELECT password_hash, salt FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if row is None:
        raise ValueError("Account not found.")
    stored_hash = row["password_hash"]
    salt = row["salt"]
    expected = _hash_password(current_password, salt)
    if not secrets.compare_digest(stored_hash, expected):
        raise ValueError("Current password is incorrect.")

    new_salt = secrets.token_hex(32)
    new_hash = _hash_password(new_password, new_salt)
    with _conn() as conn:
        conn.execute(
            "UPDATE users SET password_hash = ?, salt = ? WHERE user_id = ?",
            (new_hash, new_salt, user_id),
        )
        # Invalidate every API key so the user has to log in again everywhere.
        conn.execute(
            "UPDATE api_keys SET is_active = 0 WHERE user_id = ? AND is_active = 1",
            (user_id,),
        )


def set_stripe_customer_id(user_id: str, customer_id: str) -> None:
    """Persist the Stripe customer ID created on the first SetupIntent."""
    if not customer_id:
        raise ValueError("customer_id is required.")
    with _conn() as conn:
        conn.execute(
            "UPDATE users SET stripe_customer_id = ? WHERE user_id = ?",
            (customer_id, user_id),
        )


def get_stripe_customer_id(user_id: str) -> str | None:
    """Return the Stripe customer ID for a user, or None if not yet created."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT stripe_customer_id FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if row is None:
        return None
    return row["stripe_customer_id"] or None
