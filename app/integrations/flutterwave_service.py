"""Flutterwave integration — payments, virtual accounts, transfers."""

import hmac
import hashlib
import httpx
import uuid
from decimal import Decimal
from typing import Optional

from app.config import get_settings

settings = get_settings()

FLW_BASE = "https://api.flutterwave.com/v3"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.FLW_SECRET_KEY}",
        "Content-Type": "application/json",
    }


def generate_reference() -> str:
    return f"GFDFLW-{uuid.uuid4().hex[:14].upper()}"


# ── Initialize payment ────────────────────────────────────────────────────────

async def initialize_payment(
    email: str,
    full_name: str,
    phone: Optional[str] = None,
    amount_naira: Decimal = Decimal("0"),
    reference: str = "",
    callback_url: Optional[str] = None,
    meta: Optional[dict] = None,
) -> dict:
    """
    Initialize a Flutterwave Standard payment.
    Returns: { payment_url, reference }
    """
    payload = {
        "tx_ref": reference,
        "amount": float(amount_naira),
        "currency": "NGN",
        "redirect_url": callback_url or f"{settings.FRONTEND_URL}/wallet?ref={reference}",
        "customer": {
            "email": email,
            "name": full_name,
            "phonenumber": phone or "",
        },
        "customizations": {
            "title": "GFD Wallet Top-up",
            "logo": "https://www.globalfd.xyz/logo.png",
        },
        "meta": meta or {},
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{FLW_BASE}/payments",
            json=payload,
            headers=_headers(),
        )
        data = resp.json()

    if data.get("status") != "success":
        raise ValueError(data.get("message", "Flutterwave initialization failed"))

    return {
        "payment_url": data["data"]["link"],
        "reference": reference,
    }


# ── Verify transaction ────────────────────────────────────────────────────────

async def verify_payment(reference: str) -> dict:
    """Verify a Flutterwave transaction by tx_ref."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{FLW_BASE}/transactions",
            params={"tx_ref": reference},
            headers=_headers(),
        )
        data = resp.json()

    if data.get("status") != "success" or not data.get("data"):
        return {"success": False, "message": data.get("message", "Verification failed")}

    tx = data["data"][0]
    return {
        "success": tx["status"] == "successful",
        "amount_naira": Decimal(str(tx["amount"])),
        "email": tx["customer"]["email"],
        "status": tx["status"],
        "channel": tx.get("payment_type"),
        "paid_at": tx.get("created_at"),
        "meta": tx.get("meta", {}),
    }


# ── Webhook signature ─────────────────────────────────────────────────────────

def verify_webhook_signature(payload: bytes, signature: str) -> bool:
    """Verify Flutterwave webhook using secret hash."""
    if not settings.FLW_WEBHOOK_HASH:
        return False
    return hmac.compare_digest(settings.FLW_WEBHOOK_HASH, signature)


# ── Virtual Account (Flutterwave Permanent Virtual Account) ───────────────────

async def create_virtual_account(
    email: str,
    full_name: str,
    bvn: Optional[str] = None,
    phone: Optional[str] = None,
    narration: Optional[str] = None,
) -> dict:
    """Create a permanent virtual account (PVA)."""
    payload = {
        "email": email,
        "is_permanent": True,
        "bvn": bvn or "",
        "phonenumber": phone or "",
        "firstname": full_name.split()[0],
        "lastname": full_name.split()[-1] if len(full_name.split()) > 1 else "",
        "narration": narration or full_name,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{FLW_BASE}/virtual-account-numbers",
            json=payload,
            headers=_headers(),
        )
        data = resp.json()

    if data.get("status") != "success":
        raise ValueError(data.get("message", "Failed to create virtual account"))

    dva = data["data"]
    return {
        "bank_name": dva.get("bank_name", ""),
        "account_name": dva.get("account_name", full_name),
        "account_number": dva.get("account_number", ""),
        "dva_id": str(dva.get("order_ref", "")),
    }


# ── Bank transfer (payout) ────────────────────────────────────────────────────

async def initiate_transfer(
    amount_naira: Decimal,
    account_bank: str,
    account_number: str,
    account_name: str,
    reference: str,
    narration: str = "GFD Wallet Withdrawal",
) -> dict:
    """Initiate a Flutterwave transfer (payout)."""
    payload = {
        "account_bank": account_bank,
        "account_number": account_number,
        "amount": float(amount_naira),
        "currency": "NGN",
        "reference": reference,
        "callback_url": f"{settings.FRONTEND_URL}/wallet",
        "debit_currency": "NGN",
        "narration": narration,
        "beneficiary_name": account_name,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{FLW_BASE}/transfers",
            json=payload,
            headers=_headers(),
        )
        data = resp.json()

    if data.get("status") != "success":
        raise ValueError(data.get("message", "Transfer initiation failed"))

    return {
        "transfer_id": str(data["data"]["id"]),
        "status": data["data"]["status"],
        "reference": data["data"]["reference"],
    }


# ── List banks ────────────────────────────────────────────────────────────────

async def list_banks(country: str = "NG") -> list:
    """Return list of banks for transfers."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{FLW_BASE}/banks/{country}",
            headers=_headers(),
        )
        data = resp.json()

    if data.get("status") != "success":
        return []

    return [
        {"name": b["name"], "code": b["code"]}
        for b in data.get("data", [])
    ]
