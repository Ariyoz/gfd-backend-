"""Paystack integration — payments, virtual accounts, transfers."""

import hmac
import hashlib
import httpx
import uuid
from decimal import Decimal
from typing import Optional

from app.config import get_settings

settings = get_settings()

PAYSTACK_BASE = "https://api.paystack.co"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
        "Content-Type": "application/json",
    }


def generate_reference() -> str:
    return f"GFD-{uuid.uuid4().hex[:16].upper()}"


# ── Initialize transaction ────────────────────────────────────────────────────

async def initialize_payment(
    email: str,
    amount_naira: Decimal,
    reference: str,
    callback_url: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> dict:
    """
    Initialize a Paystack payment.
    Returns: { authorization_url, reference, access_code }
    """
    payload = {
        "email": email,
        "amount": int(amount_naira * 100),   # Paystack uses kobo
        "reference": reference,
        "callback_url": callback_url or f"{settings.FRONTEND_URL}/wallet?ref={reference}",
        "metadata": metadata or {},
        "channels": ["card", "bank", "ussd", "bank_transfer"],
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{PAYSTACK_BASE}/transaction/initialize",
            json=payload,
            headers=_headers(),
        )
        data = resp.json()

    if not data.get("status"):
        raise ValueError(data.get("message", "Paystack initialization failed"))

    return {
        "payment_url": data["data"]["authorization_url"],
        "reference": data["data"]["reference"],
        "access_code": data["data"]["access_code"],
    }


# ── Verify transaction ────────────────────────────────────────────────────────

async def verify_payment(reference: str) -> dict:
    """
    Verify a Paystack transaction.
    Returns: { success, amount_naira, email, status, channel, paid_at }
    """
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{PAYSTACK_BASE}/transaction/verify/{reference}",
            headers=_headers(),
        )
        data = resp.json()

    if not data.get("status"):
        return {"success": False, "message": data.get("message", "Verification failed")}

    tx = data["data"]
    return {
        "success": tx["status"] == "success",
        "amount_naira": Decimal(tx["amount"]) / 100,
        "email": tx["customer"]["email"],
        "status": tx["status"],
        "channel": tx.get("channel"),
        "paid_at": tx.get("paid_at"),
        "metadata": tx.get("metadata", {}),
    }


# ── Webhook signature ─────────────────────────────────────────────────────────

def verify_webhook_signature(payload: bytes, signature: str) -> bool:
    """Verify that a webhook came from Paystack."""
    if not settings.PAYSTACK_SECRET_KEY:
        return False
    expected = hmac.new(
        settings.PAYSTACK_SECRET_KEY.encode(),
        payload,
        hashlib.sha512,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


# ── Create customer ───────────────────────────────────────────────────────────

async def create_customer(email: str, full_name: str, phone: Optional[str] = None) -> dict:
    """Create a Paystack customer and return customer_code."""
    first, *rest = full_name.strip().split(" ", 1)
    last = rest[0] if rest else ""
    payload = {
        "email": email,
        "first_name": first,
        "last_name": last,
    }
    if phone:
        payload["phone"] = phone

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{PAYSTACK_BASE}/customer",
            json=payload,
            headers=_headers(),
        )
        data = resp.json()

    if not data.get("status"):
        raise ValueError(data.get("message", "Failed to create customer"))

    return {
        "customer_code": data["data"]["customer_code"],
        "id": data["data"]["id"],
    }


# ── Create Dedicated Virtual Account (DVA) ────────────────────────────────────

async def create_virtual_account(customer_code: str, preferred_bank: str = "wema-bank") -> dict:
    """
    Create a Dedicated Virtual Account for a customer.
    preferred_bank: wema-bank | titan-paystack
    """
    payload = {
        "customer": customer_code,
        "preferred_bank": preferred_bank,
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{PAYSTACK_BASE}/dedicated_account",
            json=payload,
            headers=_headers(),
        )
        data = resp.json()

    if not data.get("status"):
        raise ValueError(data.get("message", "Failed to create virtual account"))

    dva = data["data"]
    return {
        "bank_name": dva["bank"]["name"],
        "account_name": dva["account_name"],
        "account_number": dva["account_number"],
        "dva_id": str(dva["id"]),
    }


# ── Initiate transfer (payout) ────────────────────────────────────────────────

async def create_transfer_recipient(
    account_name: str,
    account_number: str,
    bank_code: str,
) -> str:
    """Create a transfer recipient and return the recipient_code."""
    payload = {
        "type": "nuban",
        "name": account_name,
        "account_number": account_number,
        "bank_code": bank_code,
        "currency": "NGN",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{PAYSTACK_BASE}/transferrecipient",
            json=payload,
            headers=_headers(),
        )
        data = resp.json()

    if not data.get("status"):
        raise ValueError(data.get("message", "Failed to create transfer recipient"))

    return data["data"]["recipient_code"]


async def initiate_transfer(
    amount_naira: Decimal,
    recipient_code: str,
    reference: str,
    reason: str = "GFD Wallet Withdrawal",
) -> dict:
    """Initiate a Paystack transfer (payout)."""
    payload = {
        "source": "balance",
        "amount": int(amount_naira * 100),
        "recipient": recipient_code,
        "reason": reason,
        "reference": reference,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{PAYSTACK_BASE}/transfer",
            json=payload,
            headers=_headers(),
        )
        data = resp.json()

    if not data.get("status"):
        raise ValueError(data.get("message", "Transfer initiation failed"))

    return {
        "transfer_code": data["data"]["transfer_code"],
        "status": data["data"]["status"],
        "reference": data["data"]["reference"],
    }


# ── List banks ────────────────────────────────────────────────────────────────

async def list_banks(country: str = "nigeria") -> list:
    """Return list of supported banks."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{PAYSTACK_BASE}/bank",
            params={"country": country, "perPage": 200},
            headers=_headers(),
        )
        data = resp.json()

    if not data.get("status"):
        return []

    return [
        {"name": b["name"], "code": b["code"], "slug": b.get("slug", "")}
        for b in data["data"]
    ]


# ── Resolve bank account number → account name ────────────────────────────────

async def resolve_account(account_number: str, bank_code: str) -> str:
    """Verify an account number and return the account name."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{PAYSTACK_BASE}/bank/resolve",
            params={"account_number": account_number, "bank_code": bank_code},
            headers=_headers(),
        )
        data = resp.json()

    if not data.get("status"):
        raise ValueError(data.get("message", "Could not verify account number"))

    return data["data"]["account_name"]
