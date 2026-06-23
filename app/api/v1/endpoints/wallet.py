"""
Wallet endpoints — full Paystack integration.

Flow:
  Fund:     POST /wallet/initialize  → Paystack page → redirect back → POST /wallet/verify
  Withdraw: POST /wallet/withdraw    → creates Paystack transfer recipient → initiates transfer
  Webhook:  POST /wallet/webhook     → Paystack calls this automatically for charge.success / transfer.success
  Admin:    GET  /wallet/admin/pending-withdrawals
            POST /wallet/admin/approve-withdrawal/{ref}
"""

import hmac
import hashlib
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from uuid import uuid4
from datetime import datetime

from app.database import get_db
from app.models import User
from app.core.dependencies import get_current_active_user, require_admin
from app.config import get_settings

router   = APIRouter()
settings = get_settings()
PS_BASE  = "https://api.paystack.co"

# Alias for readability
get_current_admin_user = require_admin


# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _ps_headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}",
        "Content-Type": "application/json",
    }


async def _get_or_create_wallet(user_id: str, db: AsyncSession) -> dict:
    """Return wallet row, creating one if it doesn't exist."""
    r = await db.execute(
        text("""
            SELECT id, balance, total_earned, total_withdrawn
            FROM wallets
            WHERE user_id = CAST(:uid AS UUID)
        """),
        {"uid": user_id},
    )
    row = r.fetchone()
    if row:
        return {
            "id": str(row[0]),
            "balance": float(row[1] or 0),
            "total_earned": float(row[2] or 0),
            "total_withdrawn": float(row[3] or 0),
        }
    wid = str(uuid4())
    await db.execute(
        text("""
            INSERT INTO wallets (id, user_id, balance, total_earned, total_withdrawn, created_at)
            VALUES (CAST(:id AS UUID), CAST(:uid AS UUID), 0, 0, 0, NOW())
        """),
        {"id": wid, "uid": user_id},
    )
    return {"id": wid, "balance": 0.0, "total_earned": 0.0, "total_withdrawn": 0.0}


def _verify_paystack_signature(body: bytes, signature: str) -> bool:
    """HMAC-SHA512 signature verification for Paystack webhooks."""
    if not settings.PAYSTACK_SECRET_KEY:
        return True  # skip in dev if key not set
    expected = hmac.new(
        settings.PAYSTACK_SECRET_KEY.encode("utf-8"),
        body,
        hashlib.sha512,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


# ══════════════════════════════════════════════════════════════════════════════
#  GET /wallet  —  balance + stats
# ══════════════════════════════════════════════════════════════════════════════

@router.get("")
async def get_wallet(
    user: User = Depends(get_current_active_user),
    db:   AsyncSession = Depends(get_db),
):
    wallet = await _get_or_create_wallet(str(user.id), db)

    # Monthly earnings (deposits + incoming earnings this calendar month)
    r = await db.execute(
        text("""
            SELECT COALESCE(SUM(amount), 0)
            FROM wallet_transactions
            WHERE wallet_id = CAST(:wid AS UUID)
              AND type IN ('deposit', 'earning')
              AND status = 'success'
              AND created_at >= date_trunc('month', NOW())
        """),
        {"wid": wallet["id"]},
    )
    monthly = float(r.scalar() or 0)

    return {**wallet, "monthly_earnings": monthly}


# ══════════════════════════════════════════════════════════════════════════════
#  GET /wallet/transactions
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/transactions")
async def get_transactions(
    user: User = Depends(get_current_active_user),
    db:   AsyncSession = Depends(get_db),
):
    wallet = await _get_or_create_wallet(str(user.id), db)
    r = await db.execute(
        text("""
            SELECT id, type, amount, description, reference, status, created_at
            FROM wallet_transactions
            WHERE wallet_id = CAST(:wid AS UUID)
            ORDER BY created_at DESC
            LIMIT 100
        """),
        {"wid": wallet["id"]},
    )
    rows = r.fetchall()
    return {
        "transactions": [
            {
                "id":          str(row[0]),
                "type":        row[1],
                "amount":      float(row[2] or 0),
                "description": row[3] or "",
                "reference":   row[4] or "",
                "status":      row[5] or "pending",
                "created_at":  str(row[6]),
            }
            for row in rows
        ]
    }


# ══════════════════════════════════════════════════════════════════════════════
#  POST /wallet/initialize  —  start Paystack payment
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/initialize")
async def initialize_payment(
    data: dict,
    user: User = Depends(get_current_active_user),
    db:   AsyncSession = Depends(get_db),
):
    """
    Call Paystack Transaction Initialize.
    Returns { authorization_url, access_code, reference }.
    Frontend redirects user to authorization_url.
    """
    amount_kobo = data.get("amount", 0)
    try:
        amount_naira = float(amount_kobo)
    except (ValueError, TypeError):
        raise HTTPException(400, "Invalid amount")

    if amount_naira < 100:
        raise HTTPException(400, "Minimum deposit is ₦100")

    wallet    = await _get_or_create_wallet(str(user.id), db)
    reference = f"gfd-{uuid4().hex[:20]}"
    frontend_url = getattr(settings, 'FRONTEND_URL', 'https://www.globalfd.xyz')
    callback  = data.get(
        "callback_url",
        f"{frontend_url}/wallet",
    )

    payload = {
        "email":        user.email,
        "amount":       int(amount_naira * 100),  # Paystack uses kobo
        "reference":    reference,
        "callback_url": callback,
        "channels":     ["card", "bank", "ussd", "bank_transfer"],
        "metadata": {
            "wallet_id": wallet["id"],
            "user_id":   str(user.id),
            "custom_fields": [
                {"display_name": "Platform", "variable_name": "platform", "value": "GFD"},
                {"display_name": "User",     "variable_name": "user_email", "value": user.email},
            ],
        },
    }

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            f"{PS_BASE}/transaction/initialize",
            json=payload,
            headers=_ps_headers(),
        )

    if resp.status_code != 200:
        raise HTTPException(502, f"Paystack error: {resp.text}")

    ps = resp.json()
    if not ps.get("status"):
        raise HTTPException(502, ps.get("message", "Paystack rejected the request"))

    ps_data = ps["data"]

    # Record a pending transaction so we can track it
    await db.execute(
        text("""
            INSERT INTO wallet_transactions
                (id, wallet_id, type, amount, description, reference, status, created_at)
            VALUES
                (gen_random_uuid(), CAST(:wid AS UUID), 'deposit', :amt,
                 'Wallet top-up via Paystack', :ref, 'pending', NOW())
        """),
        {"wid": wallet["id"], "amt": amount_naira, "ref": reference},
    )

    return {
        "authorization_url": ps_data["authorization_url"],
        "access_code":       ps_data["access_code"],
        "reference":         reference,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  POST /wallet/verify  —  verify after Paystack redirect
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/verify")
async def verify_payment(
    data: dict,
    user: User = Depends(get_current_active_user),
    db:   AsyncSession = Depends(get_db),
):
    """
    Called after Paystack redirects back.
    Verifies payment server-side and credits wallet.
    """
    reference = (data.get("reference") or "").strip()
    if not reference:
        raise HTTPException(400, "reference is required")

    # Idempotency: already credited?
    r = await db.execute(
        text("SELECT status FROM wallet_transactions WHERE reference = :ref"),
        {"ref": reference},
    )
    tx = r.fetchone()
    if tx and tx[0] == "success":
        return {"message": "Already verified", "credited": False}

    # Verify with Paystack
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get(
            f"{PS_BASE}/transaction/verify/{reference}",
            headers=_ps_headers(),
        )

    if resp.status_code != 200:
        raise HTTPException(502, f"Paystack error: {resp.text}")

    ps   = resp.json()
    data_ps = ps.get("data", {})

    if data_ps.get("status") != "success":
        raise HTTPException(400, f"Payment not successful: {data_ps.get('gateway_response', 'unknown')}")

    amount_naira = data_ps["amount"] / 100
    wallet       = await _get_or_create_wallet(str(user.id), db)

    # Credit wallet balance
    await db.execute(
        text("""
            UPDATE wallets
            SET balance      = balance + :amt,
                total_earned = total_earned + :amt
            WHERE id = CAST(:wid AS UUID)
        """),
        {"amt": amount_naira, "wid": wallet["id"]},
    )

    # Mark transaction as success (or insert if webhook hasn't run yet)
    updated = await db.execute(
        text("""
            UPDATE wallet_transactions
            SET status = 'success'
            WHERE reference = :ref
            RETURNING id
        """),
        {"ref": reference},
    )
    if not updated.fetchone():
        await db.execute(
            text("""
                INSERT INTO wallet_transactions
                    (id, wallet_id, type, amount, description, reference, status, created_at)
                VALUES
                    (gen_random_uuid(), CAST(:wid AS UUID), 'deposit', :amt,
                     'Wallet top-up via Paystack', :ref, 'success', NOW())
            """),
            {"wid": wallet["id"], "amt": amount_naira, "ref": reference},
        )

    return {
        "message": f"₦{amount_naira:,.2f} successfully credited to your wallet",
        "credited": True,
        "amount":   amount_naira,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  GET /wallet/banks  —  Paystack bank list for Nigeria
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/banks")
async def get_banks(user: User = Depends(get_current_active_user)):
    """Fetch live Nigerian bank list from Paystack."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{PS_BASE}/bank?country=nigeria&use_cursor=false&perPage=100",
            headers=_ps_headers(),
        )
    if resp.status_code != 200:
        raise HTTPException(502, "Could not fetch bank list")
    banks = resp.json().get("data", [])
    return {
        "banks": [
            {"name": b["name"], "code": b["code"], "slug": b.get("slug", "")}
            for b in banks
        ]
    }


# ══════════════════════════════════════════════════════════════════════════════
#  POST /wallet/verify-account  —  verify account number before withdrawal
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/verify-account")
async def verify_bank_account(
    data: dict,
    user: User = Depends(get_current_active_user),
):
    """
    Resolve account number + bank code to get account name via Paystack.
    Called before submitting withdrawal so user can confirm their account name.
    """
    account_number = data.get("account_number", "").strip()
    bank_code      = data.get("bank_code", "").strip()

    if len(account_number) != 10:
        raise HTTPException(400, "Account number must be 10 digits")
    if not bank_code:
        raise HTTPException(400, "Bank code is required")

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{PS_BASE}/bank/resolve?account_number={account_number}&bank_code={bank_code}",
            headers=_ps_headers(),
        )

    if resp.status_code != 200:
        raise HTTPException(400, "Could not resolve account. Check account number and bank.")

    ps = resp.json()
    if not ps.get("status"):
        raise HTTPException(400, ps.get("message", "Account resolution failed"))

    return {
        "account_name":   ps["data"]["account_name"],
        "account_number": ps["data"]["account_number"],
    }


# ══════════════════════════════════════════════════════════════════════════════
#  POST /wallet/withdraw  —  initiate Paystack transfer
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/withdraw")
async def request_withdrawal(
    data: dict,
    user: User = Depends(get_current_active_user),
    db:   AsyncSession = Depends(get_db),
):
    """
    Full Paystack Transfers flow:
      1. Create transfer recipient
      2. Initiate transfer
      3. Deduct wallet balance (held pending transfer confirmation)
    """
    amount         = float(data.get("amount", 0))
    bank_code      = data.get("bank_code", "").strip()
    account_number = data.get("account_number", "").strip()
    account_name   = data.get("account_name", "").strip()

    if amount < 500:
        raise HTTPException(400, "Minimum withdrawal is ₦500")
    if not bank_code or len(account_number) != 10 or not account_name:
        raise HTTPException(400, "bank_code, 10-digit account_number and account_name are required")

    wallet = await _get_or_create_wallet(str(user.id), db)
    if wallet["balance"] < amount:
        raise HTTPException(
            400,
            f"Insufficient balance. Available: ₦{wallet['balance']:,.2f}",
        )

    reference = f"wd-gfd-{uuid4().hex[:16]}"

    # ── Step 1: Create Paystack transfer recipient ──
    async with httpx.AsyncClient(timeout=20) as client:
        rec_resp = await client.post(
            f"{PS_BASE}/transferrecipient",
            json={
                "type":           "nuban",
                "name":           account_name,
                "account_number": account_number,
                "bank_code":      bank_code,
                "currency":       "NGN",
            },
            headers=_ps_headers(),
        )

    if rec_resp.status_code not in (200, 201):
        raise HTTPException(502, f"Could not create transfer recipient: {rec_resp.text}")

    rec_data = rec_resp.json()
    if not rec_data.get("status"):
        raise HTTPException(400, rec_data.get("message", "Recipient creation failed"))

    recipient_code = rec_data["data"]["recipient_code"]

    # ── Step 2: Initiate transfer ──
    async with httpx.AsyncClient(timeout=20) as client:
        tf_resp = await client.post(
            f"{PS_BASE}/transfer",
            json={
                "source":    "balance",
                "amount":    int(amount * 100),   # kobo
                "recipient": recipient_code,
                "reference": reference,
                "reason":    f"GFD Wallet Withdrawal — {user.email}",
            },
            headers=_ps_headers(),
        )

    if tf_resp.status_code not in (200, 201):
        raise HTTPException(502, f"Transfer initiation failed: {tf_resp.text}")

    tf_data = tf_resp.json()
    transfer_status = tf_data.get("data", {}).get("status", "pending")

    # ── Step 3: Deduct from wallet immediately (funds are held) ──
    await db.execute(
        text("""
            UPDATE wallets
            SET balance         = balance - :amt,
                total_withdrawn = total_withdrawn + :amt
            WHERE id = CAST(:wid AS UUID)
        """),
        {"amt": amount, "wid": wallet["id"]},
    )

    # Record withdrawal transaction
    await db.execute(
        text("""
            INSERT INTO wallet_transactions
                (id, wallet_id, type, amount, description, reference, status, created_at)
            VALUES
                (gen_random_uuid(), CAST(:wid AS UUID), 'withdrawal', :amt,
                 :desc, :ref, :st, NOW())
        """),
        {
            "wid":  wallet["id"],
            "amt":  amount,
            "desc": f"Withdrawal → {account_name} ({account_number})",
            "ref":  reference,
            "st":   transfer_status,   # pending / success / otp
        },
    )

    return {
        "message":   "Withdrawal initiated. Funds will arrive within minutes.",
        "reference": reference,
        "status":    transfer_status,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  POST /wallet/webhook  —  Paystack sends events here
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/webhook")
async def paystack_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    """
    Receives Paystack webhook events.
    Register this URL in your Paystack Dashboard → Settings → Webhooks.
    URL: https://gfd-backend.onrender.com/api/v1/wallet/webhook
    """
    body = await request.body()
    sig  = request.headers.get("x-paystack-signature", "")

    if not _verify_paystack_signature(body, sig):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid webhook signature")

    try:
        event = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")

    ev_type = event.get("event", "")
    ev_data = event.get("data", {})

    # ── charge.success  (card/bank payment completed) ──
    if ev_type == "charge.success":
        reference    = ev_data.get("reference", "")
        amount_naira = ev_data.get("amount", 0) / 100
        meta         = ev_data.get("metadata", {})
        wallet_id    = meta.get("wallet_id")

        if wallet_id and reference:
            r = await db.execute(
                text("SELECT status FROM wallet_transactions WHERE reference = :ref"),
                {"ref": reference},
            )
            tx = r.fetchone()
            if not tx or tx[0] != "success":
                await db.execute(
                    text("""
                        UPDATE wallets
                        SET balance      = balance + :amt,
                            total_earned = total_earned + :amt
                        WHERE id = CAST(:wid AS UUID)
                    """),
                    {"amt": amount_naira, "wid": wallet_id},
                )
                await db.execute(
                    text("""
                        UPDATE wallet_transactions
                        SET status = 'success'
                        WHERE reference = :ref
                    """),
                    {"ref": reference},
                )

    # ── transfer.success  (withdrawal completed) ──
    elif ev_type == "transfer.success":
        reference = ev_data.get("reference", "")
        if reference:
            await db.execute(
                text("""
                    UPDATE wallet_transactions
                    SET status = 'success'
                    WHERE reference = :ref AND type = 'withdrawal'
                """),
                {"ref": reference},
            )

    # ── transfer.failed / transfer.reversed  (withdrawal failed — refund) ──
    elif ev_type in ("transfer.failed", "transfer.reversed"):
        reference    = ev_data.get("reference", "")
        amount_naira = ev_data.get("amount", 0) / 100

        if reference and amount_naira > 0:
            # Refund wallet
            r = await db.execute(
                text("""
                    SELECT wt.wallet_id
                    FROM wallet_transactions wt
                    WHERE wt.reference = :ref AND wt.type = 'withdrawal'
                """),
                {"ref": reference},
            )
            row = r.fetchone()
            if row:
                wallet_id = str(row[0])
                await db.execute(
                    text("""
                        UPDATE wallets
                        SET balance         = balance + :amt,
                            total_withdrawn = total_withdrawn - :amt
                        WHERE id = CAST(:wid AS UUID)
                    """),
                    {"amt": amount_naira, "wid": wallet_id},
                )
                await db.execute(
                    text("""
                        UPDATE wallet_transactions
                        SET status = :st
                        WHERE reference = :ref
                    """),
                    {"st": ev_type.replace("transfer.", ""), "ref": reference},
                )

    return {"status": "ok"}


# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN endpoints
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/admin/overview")
async def admin_wallet_overview(
    _: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Admin: total platform wallet stats."""
    r = await db.execute(text("""
        SELECT
            COUNT(*)                           AS total_wallets,
            COALESCE(SUM(balance), 0)          AS total_balance,
            COALESCE(SUM(total_earned), 0)     AS platform_earned,
            COALESCE(SUM(total_withdrawn), 0)  AS platform_withdrawn
        FROM wallets
    """))
    row = r.fetchone()
    return {
        "total_wallets":      int(row[0] or 0),
        "total_balance":      float(row[1] or 0),
        "platform_earned":    float(row[2] or 0),
        "platform_withdrawn": float(row[3] or 0),
    }


@router.get("/admin/pending-withdrawals")
async def admin_pending_withdrawals(
    _: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Admin: list all pending withdrawals."""
    r = await db.execute(text("""
        SELECT
            wt.id, wt.reference, wt.amount, wt.description,
            wt.status, wt.created_at,
            u.email, u.full_name
        FROM wallet_transactions wt
        JOIN wallets w  ON w.id = wt.wallet_id
        JOIN users u    ON u.id = w.user_id
        WHERE wt.type = 'withdrawal' AND wt.status = 'pending'
        ORDER BY wt.created_at ASC
    """))
    rows = r.fetchall()
    return {
        "withdrawals": [
            {
                "id":          str(row[0]),
                "reference":   row[1],
                "amount":      float(row[2] or 0),
                "description": row[3],
                "status":      row[4],
                "created_at":  str(row[5]),
                "user_email":  row[6],
                "user_name":   row[7],
            }
            for row in rows
        ]
    }


@router.post("/admin/credit-user")
async def admin_credit_user(
    data: dict,
    _: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """Admin: manually credit a user's wallet (e.g., job completion payment)."""
    user_id     = data.get("user_id", "")
    amount      = float(data.get("amount", 0))
    description = data.get("description", "Admin credit")

    if amount <= 0:
        raise HTTPException(400, "Amount must be positive")

    wallet = await _get_or_create_wallet(user_id, db)

    await db.execute(
        text("""
            UPDATE wallets
            SET balance      = balance + :amt,
                total_earned = total_earned + :amt
            WHERE id = CAST(:wid AS UUID)
        """),
        {"amt": amount, "wid": wallet["id"]},
    )

    ref = f"admin-credit-{uuid4().hex[:12]}"
    await db.execute(
        text("""
            INSERT INTO wallet_transactions
                (id, wallet_id, type, amount, description, reference, status, created_at)
            VALUES
                (gen_random_uuid(), CAST(:wid AS UUID), 'earning', :amt,
                 :desc, :ref, 'success', NOW())
        """),
        {"wid": wallet["id"], "amt": amount, "desc": description, "ref": ref},
    )

    return {"message": f"₦{amount:,.2f} credited to user wallet", "reference": ref}
