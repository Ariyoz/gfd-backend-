"""Email background tasks using Resend."""

from .celery_app import celery_app


@celery_app.task(name="send_welcome_email")
def send_welcome_email(email: str, name: str):
    """Send welcome email to new user."""
    import resend
    from app.config import get_settings
    settings = get_settings()
    resend.api_key = settings.RESEND_API_KEY

    resend.Emails.send({
        "from": settings.RESEND_FROM_EMAIL,
        "to": email,
        "subject": f"Welcome to GFD, {name}!",
        "html": f"<h1>Welcome to Global Fullstack Developers!</h1><p>Hi {name}, your account is ready.</p>",
    })


@celery_app.task(name="send_verification_email")
def send_verification_email(email: str, token: str):
    """Send email verification link."""
    import resend
    from app.config import get_settings
    settings = get_settings()
    resend.api_key = settings.RESEND_API_KEY

    verify_url = f"https://gfd.dev/verify?token={token}"
    resend.Emails.send({
        "from": settings.RESEND_FROM_EMAIL,
        "to": email,
        "subject": "Verify your GFD email",
        "html": f"<p>Click <a href='{verify_url}'>here</a> to verify your email.</p>",
    })


@celery_app.task(name="send_password_reset_email")
def send_password_reset_email(email: str, token: str):
    """Send password reset email."""
    import resend
    from app.config import get_settings
    settings = get_settings()
    resend.api_key = settings.RESEND_API_KEY

    reset_url = f"https://gfd.dev/reset-password?token={token}"
    resend.Emails.send({
        "from": settings.RESEND_FROM_EMAIL,
        "to": email,
        "subject": "Reset your GFD password",
        "html": f"<p>Click <a href='{reset_url}'>here</a> to reset your password. Link expires in 1 hour.</p>",
    })
