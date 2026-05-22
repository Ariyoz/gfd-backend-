"""Resend email service integration."""

import resend
from app.config import get_settings

settings = get_settings()
resend.api_key = settings.RESEND_API_KEY


class EmailService:
    """Email service using Resend."""

    FROM_EMAIL = settings.RESEND_FROM_EMAIL
    BASE_URL = "https://gfd.dev"

    @classmethod
    def send(cls, to: str, subject: str, html: str):
        """Send an email."""
        return resend.Emails.send({
            "from": cls.FROM_EMAIL,
            "to": to,
            "subject": subject,
            "html": html,
        })

    @classmethod
    def send_welcome(cls, email: str, name: str):
        """Welcome email for new users."""
        cls.send(
            to=email,
            subject=f"Welcome to GFD, {name}! 🚀",
            html=f"""
            <div style="font-family:sans-serif;max-width:600px;margin:0 auto;">
                <h1 style="color:#630ed4;">Welcome to GFD!</h1>
                <p>Hi {name},</p>
                <p>Your account on <strong>Global Fullstack Developers</strong> is ready.</p>
                <p>Here's what you can do:</p>
                <ul>
                    <li>Connect your GitHub to showcase your work</li>
                    <li>Build your developer profile</li>
                    <li>Post updates and connect with the community</li>
                    <li>Apply for projects and get hired</li>
                </ul>
                <a href="{cls.BASE_URL}/dashboard" style="display:inline-block;padding:12px 24px;background:#630ed4;color:#fff;text-decoration:none;border-radius:8px;margin-top:16px;">Go to Dashboard</a>
                <p style="margin-top:24px;color:#666;">— The GFD Team</p>
            </div>
            """,
        )

    @classmethod
    def send_verification(cls, email: str, token: str):
        """Email verification link."""
        verify_url = f"{cls.BASE_URL}/verify?token={token}"
        cls.send(
            to=email,
            subject="Verify your GFD email",
            html=f"""
            <div style="font-family:sans-serif;max-width:600px;margin:0 auto;">
                <h2 style="color:#630ed4;">Verify Your Email</h2>
                <p>Click the button below to verify your email address:</p>
                <a href="{verify_url}" style="display:inline-block;padding:12px 24px;background:#630ed4;color:#fff;text-decoration:none;border-radius:8px;">Verify Email</a>
                <p style="margin-top:16px;color:#666;font-size:14px;">This link expires in 24 hours.</p>
            </div>
            """,
        )

    @classmethod
    def send_password_reset(cls, email: str, token: str):
        """Password reset email."""
        reset_url = f"{cls.BASE_URL}/auth/reset-password?token={token}"
        cls.send(
            to=email,
            subject="Reset your GFD password",
            html=f"""
            <div style="font-family:sans-serif;max-width:600px;margin:0 auto;">
                <h2 style="color:#630ed4;">Reset Your Password</h2>
                <p>Click below to reset your password:</p>
                <a href="{reset_url}" style="display:inline-block;padding:12px 24px;background:#630ed4;color:#fff;text-decoration:none;border-radius:8px;">Reset Password</a>
                <p style="margin-top:16px;color:#666;font-size:14px;">This link expires in 1 hour. If you didn't request this, ignore this email.</p>
            </div>
            """,
        )

    @classmethod
    def send_application_update(cls, email: str, name: str, project_title: str, status: str):
        """Notify developer about application status change."""
        cls.send(
            to=email,
            subject=f"Application Update: {project_title}",
            html=f"""
            <div style="font-family:sans-serif;max-width:600px;margin:0 auto;">
                <h2 style="color:#630ed4;">Application Update</h2>
                <p>Hi {name},</p>
                <p>Your application for <strong>{project_title}</strong> has been <strong>{status}</strong>.</p>
                <a href="{cls.BASE_URL}/dashboard/requests" style="display:inline-block;padding:12px 24px;background:#630ed4;color:#fff;text-decoration:none;border-radius:8px;">View Details</a>
            </div>
            """,
        )

    @classmethod
    def send_new_message_notification(cls, email: str, sender_name: str):
        """Notify user of new message."""
        cls.send(
            to=email,
            subject=f"New message from {sender_name}",
            html=f"""
            <div style="font-family:sans-serif;max-width:600px;margin:0 auto;">
                <p><strong>{sender_name}</strong> sent you a message on GFD.</p>
                <a href="{cls.BASE_URL}/messaging" style="display:inline-block;padding:12px 24px;background:#630ed4;color:#fff;text-decoration:none;border-radius:8px;">Open Messages</a>
            </div>
            """,
        )
