"""Paystack integration — payments, virtual accounts, transfers."""

import hmac
import hashlib
import httpx
import uuid
import logging
from decimal import Decimal
from typing import Optional

from app.config import get_settings

logger = logging.getLogger("gfd.paystack")

PAYSTACK_BASE = "https://api.paystack.co"


def _headers() -> dict:
    s = get_settings()
    return {
        "Authorization": f"Bearer {s.PAYSTACK_SECRET_KEY}",
        "Content-Type": "application/json",
    }


def _secret_key() -> str:
    return get_settings().PAYSTACK_SECRET_KEY


def generate_reference() -> str:
    return f"GFD-{uuid.uuid4().hex[:16].upper()}"


async def initialize_payment(
    email: str,
    amount_naira: Decimal,
    reference: str,
    callback_url: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> dict:
    s = get_settings()
    if not s.PAYSTACK_SECRET_KEY:
        raise ValueError("PAYSTACK_SECRET_KEY is not configured on this server")

    cb_url = callback_url or f"{s.FRONTEND_URL}/wallet?ref={reference}"

    payload = {
        "email": email,
        "amount": int(amount_naira * 100),
        "reference": reference,
        "callback_url": cb_url,
        "metadata": metadata or {},
        "channels": ["card", "bank", "ussd", "bank_transfer"],
    }

    logger.info(f"[Paystack] Init: ref={reference} amount=₦{amount_naira} email={email}")

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{PAYSTACK_BASE}/transaction/initialize",
            json=payload,
            headers=_headers(),
        )
        data = resp.json()

    if not data.get("status"):
        msg = data.get("message", "Paystack initialization failed")
        logger.error(f"[Paystack] Init failed: {msg} ref={reference}")
        raise ValueError(msg)

    logger.info(f"[Paystack] Init OK: ref={reference}")
    return {
        "payment_url": data["data"]["authorization_url"],
        "reference": data["data"]["reference"],
        "access_code": data["data"]["access_code"],
    }


async def verify_payment(reference: str) -> dict:
    logger.info(f"[Paystack] Verifying: ref={reference}")
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{PAYSTACK_BASE}/transaction/verify/{reference}",
            headers=_headers(),
        )
        data = resp.json()

    if not data.get("status"):
        msg = data.get("message", "Verification failed")
        logger.warning(f"[Paystack] Verify failed: {msg} ref={reference}")
        return {"success": False, "message": msg}

    tx = data["data"]
    success = tx["status"] == "success"
    logger.info(f"[Paystack] Verify result: ref={reference} status={tx['status']} amount={tx['amount']}")
    return {
        "success": success,
        "amount_naira": Decimal(str(tx["amount"])) / 100,
        "email": tx["customer"]["email"],
        "status": tx["status"],
        "channel": tx.get("channel"),
        "paid_at": tx.get("paid_at"),
        "metadata": tx.get("metadata", {}),
    }


def verify_webhook_signature(payload: bytes, signature: str) -> bool:
    secret_key = _secret_key()
    if not secret_key:
        logger.warning("[Paystack] Webhook: PAYSTACK_SECRET_KEY not set")
        return False
    if not signature:
        logger.warning("[Paystack] Webhook: missing signature header")
        return False
    expected = hmac.new(
        secret_key.encode("utf-8"),
        payload,
        hashlib.sha512,
    ).hexdigest()
    result = hmac.compare_digest(expected, signature)
    if not result:
        logger.warning("[Paystack] Webhook: signature mismatch")
    return result


async def create_customer(email: str, full_name: str, phone: Optional[str] = None) -> dict:
    first, *rest = full_name.strip().split(" ", 1)
    last = rest[0] if rest else ""
    payload = {"email": email, "first_name": first, "last_name": last}
    if phone:
        payload["phone"] = phone
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{PAYSTACK_BASE}/customer", json=payload, headers=_headers())
        data = resp.json()
    if not data.get("status"):
        raise ValueError(data.get("message", "Failed to create customer"))
    return {"customer_code": data["data"]["customer_code"], "id": data["data"]["id"]}


async def create_virtual_account(customer_code: str, preferred_bank: str = "wema-bank") -> dict:
    payload = {"customer": customer_code, "preferred_bank": preferred_bank}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{PAYSTACK_BASE}/dedicated_account", json=payload, headers=_headers())
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


async def create_transfer_recipient(account_name: str, account_number: str, bank_code: str) -> str:
    payload = {
        "type": "nuban", "name": account_name,
        "account_number": account_number, "bank_code": bank_code, "currency": "NGN",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{PAYSTACK_BASE}/transferrecipient", json=payload, headers=_headers())
        data = resp.json()
    if not data.get("status"):
        raise ValueError(data.get("message", "Failed to create transfer recipient"))
    return data["data"]["recipient_code"]


async def initiate_transfer(
    amount_naira: Decimal, recipient_code: str, reference: str,
    reason: str = "GFD Wallet Withdrawal",
) -> dict:
    payload = {
        "source": "balance", "amount": int(amount_naira * 100),
        "recipient": recipient_code, "reason": reason, "reference": reference,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{PAYSTACK_BASE}/transfer", json=payload, headers=_headers())
        data = resp.json()
    if not data.get("status"):
        raise ValueError(data.get("message", "Transfer initiation failed"))
    return {
        "transfer_code": data["data"]["transfer_code"],
        "status": data["data"]["status"],
        "reference": data["data"]["reference"],
    }


async def list_banks(country: str = "nigeria") -> list:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{PAYSTACK_BASE}/bank",
            params={"country": country, "perPage": 200},
            headers=_headers(),
        )
        data = resp.json()
    if not data.get("status"):
        return []
    return [{"name": b["name"], "code": b["code"], "slug": b.get("slug", "")} for b in data["data"]]


async def resolve_account(account_number: str, bank_code: str) -> str:
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
