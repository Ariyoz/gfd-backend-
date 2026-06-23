"""Wallet endpoints — Paystack fund, verify, withdraw, history."""

import httpx
import os
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from uuid import uuid4

from app.database import get_db
from app.models import User
from app.core.dependencies import get_current_active_user

router = APIRouter()

PAYSTACK_SECRET = os.getenv("PAYSTACK_SECRET_KEY", "")
PAYSTACK_BASE   = "https://api.paystack.co"


# ── helpers ──────────────────────────────────────────────────────────────────

async def _get_or_create_wallet(user_id: str, db: AsyncSession) -> dict:
    r = await db.execute(
        text("SELECT id, balance, total_earned, total_withdrawn FROM wallets WHERE user_id = CAST(:uid AS UUID)"),
        {"uid": user_id}
    )
    row = r.fetchone()
    if row:
        return {"id": str(row[0]), "balance": float(row[1] or 0),
                "total_earned": float(row[2] or 0), "total_withdrawn": float(row[3] or 0)}
    wid = str(uuid4())
    await db.execute(
        text("INSERT INTO wallets (id, user_id, balance, total_earned, total_withdrawn, created_at) "
             "VALUES (CAST(:id AS UUID), CAST(:uid AS UUID), 0, 0, 0, NOW())"),
        {"id": wid, "uid": user_id}
    )
    return {"id": wid, "balance": 0.0, "total_earned": 0.0, "total_withdrawn": 0.0}


async def _paystack_headers():
    return {"Authorization": f"Bearer {PAYSTACK_SECRET}", "Content-Type": "application/json"}


# ── GET /wallet ───────────────────────────────────────────────────────────────

@router.get("")
async def get_wallet(
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    wallet = await _get_or_create_wallet(str(user.id), db)

    # Monthly earnings
    r = await db.execute(
        text("""SELECT COALESCE(SUM(amount),0) FROM wallet_transactions
                WHERE wallet_id = CAST(:wid AS UUID)
                AND type IN ('deposit','earning')
                AND status = 'success'
                AND created_at >= date_trunc('month', NOW())"""),
        {"wid": wallet["id"]}
    )
    monthly = float(r.scalar() or 0)

    return {**wallet, "monthly_earnings": monthly}


# ── GET /wallet/transactions ──────────────────────────────────────────────────

@router.get("/transactions")
async def get_transactions(
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    wallet = await _get_or_create_wallet(str(user.id), db)
    r = await db.execute(
        text("""SELECT id, type, amount, description, reference, status, created_at
                FROM wallet_transactions
                WHERE wallet_id = CAST(:wid AS UUID)
                ORDER BY created_at DESC LIMIT 50"""),
        {"wid": wallet["id"]}
    )
    rows = r.fetchall()
    return {"transactions": [
        {
            "id": str(row[0]),
            "type": row[1],
            "amount": float(row[2] or 0),
            "description": row[3] or "",
            "reference": row[4] or "",
            "status": row[5] or "pending",
            "created_at": str(row[6]),
        }
        for row in rows
    ]}


# ── POST /wallet/initialize ───────────────────────────────────────────────────

@router.post("/initialize")
async def initialize_payment(
    data: dict,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Initialize a Paystack payment. Returns authorization_url to redirect user."""
    amount_naira = float(data.get("amount", 0))
    if amount_naira < 100:
        raise HTTPException(400, "Minimum deposit is ₦100")

    wallet = await _get_or_create_wallet(str(user.id), db)
    reference = f"gfd-{uuid4().hex[:16]}"

    # Call Paystack initialize
    payload = {
        "email": user.email,
        "amount": int(amount_naira * 100),   # Paystack uses kobo
        "reference": reference,
        "callback_url": data.get("callback_url", "https://www.globalfd.xyz/wallet?verify=1"),
        "metadata": {
            "wallet_id": wallet["id"],
            "user_id": str(user.id),
            "type": "deposit",
        },
    }

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{PAYSTACK_BASE}/transaction/initialize",
            json=payload,
            headers=await _paystack_headers(),
        )

    if resp.status_code != 200:
        raise HTTPException(502, f"Paystack error: {resp.text}")

    ps_data = resp.json().get("data", {})

    # Record pending transaction
    await db.execute(
        text("""INSERT INTO wallet_transactions
                (id, wallet_id, type, amount, description, reference, status, created_at)
                VALUES (gen_random_uuid(), CAST(:wid AS UUID), 'deposit', :amt,
                        'Wallet top-up', :ref, 'pending', NOW())"""),
        {"wid": wallet["id"], "amt": amount_naira, "ref": reference}
    )

    return {
        "authorization_url": ps_data.get("authorization_url"),
        "access_code": ps_data.get("access_code"),
        "reference": reference,
    }


# ── POST /wallet/verify ───────────────────────────────────────────────────────

@router.post("/verify")
async def verify_payment(
    data: dict,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Verify a Paystack payment and credit the wallet."""
    reference = data.get("reference", "").strip()
    if not reference:
        raise HTTPException(400, "Reference required")

    # Check already verified
    r = await db.execute(
        text("SELECT status FROM wallet_transactions WHERE reference = :ref"),
        {"ref": reference}
    )
    tx = r.fetchone()
    if tx and tx[0] == "success":
        return {"message": "Already verified", "credited": False}

    # Verify with Paystack
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{PAYSTACK_BASE}/transaction/verify/{reference}",
            headers=await _paystack_headers(),
        )

    if resp.status_code != 200:
        raise HTTPException(502, f"Paystack error: {resp.text}")

    ps = resp.json().get("data", {})
    if ps.get("status") != "success":
        raise HTTPException(400, f"Payment not successful: {ps.get('gateway_response')}")

    amount_naira = ps["amount"] / 100
    wallet = await _get_or_create_wallet(str(user.id), db)

    # Credit wallet
    await db.execute(
        text("""UPDATE wallets
                SET balance = balance + :amt,
                    total_earned = total_earned + :amt
                WHERE id = CAST(:wid AS UUID)"""),
        {"amt": amount_naira, "wid": wallet["id"]}
    )

    # Update transaction status
    await db.execute(
        text("""UPDATE wallet_transactions
                SET status = 'success'
                WHERE reference = :ref"""),
        {"ref": reference}
    )

    return {"message": f"₦{amount_naira:,.2f} credited to your wallet", "credited": True, "amount": amount_naira}


# ── POST /wallet/withdraw ─────────────────────────────────────────────────────

@router.post("/withdraw")
async def request_withdrawal(
    data: dict,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Request a withdrawal. Creates a pending withdrawal that admin processes."""
    amount = float(data.get("amount", 0))
    bank_name = data.get("bank_name", "").strip()
    account_number = data.get("account_number", "").strip()
    account_name = data.get("account_name", "").strip()

    if amount < 500:
        raise HTTPException(400, "Minimum withdrawal is ₦500")
    if not bank_name or not account_number or not account_name:
        raise HTTPException(400, "Bank name, account number and account name are required")

    wallet = await _get_or_create_wallet(str(user.id), db)

    if wallet["balance"] < amount:
        raise HTTPException(400, f"Insufficient balance. Available: ₦{wallet['balance']:,.2f}")

    reference = f"wd-{uuid4().hex[:16]}"

    # Deduct balance immediately (hold it)
    await db.execute(
        text("""UPDATE wallets
                SET balance = balance - :amt,
                    total_withdrawn = total_withdrawn + :amt
                WHERE id = CAST(:wid AS UUID)"""),
        {"amt": amount, "wid": wallet["id"]}
    )

    # Record pending withdrawal
    await db.execute(
        text("""INSERT INTO wallet_transactions
                (id, wallet_id, type, amount, description, reference, status, created_at)
                VALUES (gen_random_uuid(), CAST(:wid AS UUID), 'withdrawal', :amt,
                        :desc, :ref, 'pending', NOW())"""),
        {
            "wid": wallet["id"], "amt": amount,
            "desc": f"Withdrawal to {account_name} — {bank_name} {account_number}",
            "ref": reference,
        }
    )

    return {"message": "Withdrawal request submitted. Processed within 24 hours.", "reference": reference}


# ── Paystack Webhook ──────────────────────────────────────────────────────────

@router.post("/webhook")
async def paystack_webhook(request, db: AsyncSession = Depends(get_db)):
    """Handle Paystack webhook events (charge.success)."""
    import hmac, hashlib
    body = await request.body()
    sig = request.headers.get("x-paystack-signature", "")
    expected = hmac.new(PAYSTACK_SECRET.encode(), body, hashlib.sha512).hexdigest()
    if sig != expected:
        raise HTTPException(400, "Invalid signature")

    event = await request.json()
    if event.get("event") == "charge.success":
        data = event.get("data", {})
        reference = data.get("reference", "")
        amount_naira = data.get("amount", 0) / 100
        meta = data.get("metadata", {})
        wallet_id = meta.get("wallet_id")

        if wallet_id and reference:
            r = await db.execute(
                text("SELECT status FROM wallet_transactions WHERE reference = :ref"),
                {"ref": reference}
            )
            tx = r.fetchone()
            if not tx or tx[0] != "success":
                await db.execute(
                    text("UPDATE wallets SET balance = balance + :amt, total_earned = total_earned + :amt "
                         "WHERE id = CAST(:wid AS UUID)"),
                    {"amt": amount_naira, "wid": wallet_id}
                )
                await db.execute(
                    text("UPDATE wallet_transactions SET status = 'success' WHERE reference = :ref"),
                    {"ref": reference}
                )

    return {"status": "ok"}
