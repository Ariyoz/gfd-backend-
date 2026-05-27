"""Subscription endpoints — Plans, payments, and verification badge."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from uuid import UUID

from app.database import get_db
from app.models import User
from app.core.dependencies import get_current_active_user

router = APIRouter()


# Plan definitions
PLANS = {
    "free": {"name": "Free", "price_monthly": 0, "price_yearly": 0},
    "pro": {"name": "Pro", "price_monthly": 19, "price_yearly": 15},
    "enterprise": {"name": "Enterprise", "price_monthly": 79, "price_yearly": 63},
}


@router.get("/plans")
async def get_plans():
    """Get available subscription plans."""
    return {"plans": PLANS}


@router.get("/my-subscription")
async def get_my_subscription(user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Get current user's subscription status."""
    result = await db.execute(text("""
        SELECT plan, billing_cycle, status, started_at, expires_at, payment_reference
        FROM subscriptions
        WHERE user_id = :user_id AND status = 'active'
        ORDER BY created_at DESC LIMIT 1
    """), {"user_id": str(user.id)})
    row = result.mappings().first()

    if not row:
        return {
            "plan": "free",
            "billing_cycle": None,
            "status": "none",
            "is_verified": user.is_verified,
            "started_at": None,
            "expires_at": None,
        }

    return {
        "plan": row["plan"],
        "billing_cycle": row["billing_cycle"],
        "status": row["status"],
        "is_verified": user.is_verified,
        "started_at": str(row["started_at"]) if row["started_at"] else None,
        "expires_at": str(row["expires_at"]) if row["expires_at"] else None,
    }


@router.post("/subscribe")
async def subscribe(data: dict, user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Subscribe to a plan. For now, activates immediately (no real payment gateway).
    In production, this would initiate a Paystack/Stripe checkout session.
    """
    plan = data.get("plan")
    billing_cycle = data.get("billing_cycle", "monthly")

    if plan not in PLANS:
        raise HTTPException(status_code=400, detail="Invalid plan")

    if plan == "free":
        # Downgrade to free — cancel active subscription
        await db.execute(text("""
            UPDATE subscriptions SET status = 'cancelled'
            WHERE user_id = :user_id AND status = 'active'
        """), {"user_id": str(user.id)})

        # Remove verified badge
        await db.execute(text(
            "UPDATE users SET is_verified = FALSE WHERE id = :user_id"
        ), {"user_id": str(user.id)})

        return {"message": "Downgraded to Free plan", "plan": "free", "is_verified": False}

    # Cancel any existing active subscription
    await db.execute(text("""
        UPDATE subscriptions SET status = 'cancelled'
        WHERE user_id = :user_id AND status = 'active'
    """), {"user_id": str(user.id)})

    # Calculate expiry
    interval = "1 month" if billing_cycle == "monthly" else "1 year"

    # Create new subscription
    await db.execute(text(f"""
        INSERT INTO subscriptions (id, user_id, plan, billing_cycle, status, started_at, expires_at, created_at)
        VALUES (gen_random_uuid(), :user_id, :plan, :billing_cycle, 'active', NOW(), NOW() + INTERVAL '{interval}', NOW())
    """), {
        "user_id": str(user.id),
        "plan": plan,
        "billing_cycle": billing_cycle,
    })

    # Grant verified badge (purple tick) for Pro and Enterprise
    await db.execute(text(
        "UPDATE users SET is_verified = TRUE WHERE id = :user_id"
    ), {"user_id": str(user.id)})

    price = PLANS[plan]["price_monthly"] if billing_cycle == "monthly" else PLANS[plan]["price_yearly"]

    return {
        "message": f"Subscribed to {PLANS[plan]['name']} plan!",
        "plan": plan,
        "billing_cycle": billing_cycle,
        "price": price,
        "is_verified": True,
    }


@router.post("/cancel")
async def cancel_subscription(user: User = Depends(get_current_active_user), db: AsyncSession = Depends(get_db)):
    """Cancel current subscription."""
    result = await db.execute(text("""
        UPDATE subscriptions SET status = 'cancelled'
        WHERE user_id = :user_id AND status = 'active'
        RETURNING id
    """), {"user_id": str(user.id)})
    row = result.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="No active subscription found")

    # Remove verified badge
    await db.execute(text(
        "UPDATE users SET is_verified = FALSE WHERE id = :user_id"
    ), {"user_id": str(user.id)})

    return {"message": "Subscription cancelled. You'll retain access until the end of your billing period.", "is_verified": False}
