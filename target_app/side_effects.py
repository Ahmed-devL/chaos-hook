"""Mockable email recording within the caller's database transaction."""

from sqlalchemy.orm import Session

from target_app.models import OutboxEmail


def send_welcome_email(
    session: Session, user_id: str, dedupe_key: str | None = None
) -> OutboxEmail:
    """Queue a welcome email without committing the caller's transaction."""
    email = OutboxEmail(
        user_id=user_id, template="welcome_pro", dedupe_key=dedupe_key
    )
    session.add(email)
    return email
