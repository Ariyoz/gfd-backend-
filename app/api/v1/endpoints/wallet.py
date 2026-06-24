"""
Wallet endpoints — Flutterwave payment integration.

Fund flow:
  POST /wallet/flw/initialize  → returns payment link → user pays
  GET  /wallet/flw/verify      → called after redirect, credits wallet
  POST /wallet/flw/webhook     → Flutterwave pushes events here

Withdraw flow:
  GET  /wallet/banks           → live bank list from Flutterwave
  POST /wallet/verify-account  → resolve account number → account name
  POST /wallet/withdraw        → Flutterwave Transfer API (instant)

Admin:
  GET  /wallet/admin/overview
  GET  /wallet/admin/pending-withdrawals
  POST /wallet/admin/credit-user
"""

import hmac
import hashlib
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from uuid import uuid4

from app.database import get_db
from app.models import User
from app.core.dependencies import get_current_active_user, require_admin
from app.config import get_settings

router   = APIRouter()
settings = get_settings()
FLW_BASE = "https://api.flutterwave.com/v3"

get_current_admin_user = require_admin


# ══════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════

def _flw_headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.FLW_SECRET_KEY}",
        "Content-Type": "application/json",
    }


async def _get_or_create_wallet(user_id: str, db: AsyncSession) -> dict:
    r = await db.execute(
        text("""
            SELECT id, balance, total_earned, total_withdrawn
            FROM wallets WHERE user_id = CAST(:uid AS UUID)
        """),
        {"uid": user_id},
    )
    row = r.fetchone()
    if row:
        return {"id": str(row[0]), "balance": float(row[1] or 0),
                "total_earned": float(row[2] or 0), "total_withdrawn": float(row[3] or 0)}
    wid = str(uuid4())
    await db.execute(
        text("""
            INSERT INTO wallets (id, user_id, balance, total_earned, total_withdrawn, created_at)
            VALUES (CAST(:id AS UUID), CAST(:uid AS UUID), 0, 0, 0, NOW())
        """),
        {"id": wid, "uid": user_id},
    )
    return {"id": wid, "balance": 0.0, "total_earned": 0.0, "total_withdrawn": 0.0}


# ══════════════════════════════════════════════════════════
# PING — health check
# ══════════════════════════════════════════════════════════

@router.get("/ping")
async def ping_flw():
    """Check Flutterwave key is loaded and reachable."""
    if not settings.FLW_SECRET_KEY:
        return {"configured": False, "message": "FLW_SECRET_KEY not set"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{FLW_BASE}/banks/NG",
                headers=_flw_headers(),
            )
        if resp.status_code == 200:
            return {
                "configured": True,
                "message": "Flutterwave connected ✓",
                "key_prefix": settings.FLW_SECRET_KEY[:14] + "...",
            }
        return {"configured": False, "message": f"FLW returned {resp.status_code}: {resp.text[:200]}"}
    except Exception as e:
        return {"configured": False, "message": str(e)}


# ══════════════════════════════════════════════════════════
# GET /wallet — balance + monthly earnings
# ══════════════════════════════════════════════════════════

@router.get("")
async def get_wallet(
    user: User = Depends(get_current_active_user),
    db:   AsyncSession = Depends(get_db),
):
    wallet = await _get_or_create_wallet(str(user.id), db)
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


# ══════════════════════════════════════════════════════════
# GET /wallet/transactions
# ══════════════════════════════════════════════════════════

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
            ORDER BY created_at DESC LIMIT 100
        """),
        {"wid": wallet["id"]},
    )
    rows = r.fetchall()
    return {"transactions": [
        {"id": str(row[0]), "type": row[1], "amount": float(row[2] or 0),
         "description": row[3] or "", "reference": row[4] or "",
         "status": row[5] or "pending", "created_at": str(row[6])}
        for row in rows
    ]}


# ══════════════════════════════════════════════════════════
# POST /wallet/flw/initialize — create Flutterwave payment link
# ══════════════════════════════════════════════════════════

@router.post("/flw/initialize")
async def flw_initialize(
    data: dict,
    user: User = Depends(get_current_active_user),
    db:   AsyncSession = Depends(get_db),
):
    """
    Create a Flutterwave Standard payment link.
    Returns { payment_link, tx_ref } — frontend redirects user to payment_link.
    Supports: card, bank transfer, USSD, mobile money — all at once.
    """
    if not settings.FLW_SECRET_KEY:
        raise HTTPException(503, "Payment gateway not configured. Contact support.")

    try:
        amount = float(data.get("amount", 0))
    except (ValueError, TypeError):
        raise HTTPException(400, "Invalid amount")

    if amount < 100:
        raise HTTPException(400, "Minimum deposit is ₦100")

    wallet   = await _get_or_create_wallet(str(user.id), db)
    tx_ref   = f"gfd-{uuid4().hex[:20]}"
    frontend = getattr(settings, "FRONTEND_URL", "https://www.globalfd.xyz")
    redirect = data.get("redirect_url", f"{frontend}/wallet")

    full_name = getattr(user, "full_name", None) or user.email.split("@")[0]
    name_parts = full_name.split(" ", 1)

    payload = {
        "tx_ref":       tx_ref,
        "amount":       amount,
        "currency":     "NGN",
        "redirect_url": redirect,
        "customer": {
            "email":       user.email,
            "name":        full_name,
            "phonenumber": getattr(user, "phone", ""),
        },
        "customizations": {
            "title":       "GFD Wallet",
            "description": f"Fund GFD wallet — ₦{amount:,.0f}",
            "logo":        "https://www.globalfd.xyz/logo.png",
        },
        "meta": {
            "wallet_id": wallet["id"],
            "user_id":   str(user.id),
        },
        # Accept all channels — no DVA needed
        "payment_options": "card,banktransfer,ussd,mobilemoney",
    }

    try:
        async with httpx.AsyncClient(timeout=25) as client:
            resp = await client.post(
                f"{FLW_BASE}/payments",
                json=payload,
                headers=_flw_headers(),
            )
    except httpx.TimeoutException:
        raise HTTPException(504, "Payment gateway timed out. Please try again.")
    except Exception as e:
        raise HTTPException(502, f"Could not reach payment gateway: {e}")

    if resp.status_code != 200:
        try:
            msg = resp.json().get("message", resp.text[:300])
        except Exception:
            msg = resp.text[:300]
        raise HTTPException(502, f"Flutterwave: {msg}")

    flw = resp.json()
    if flw.get("status") != "success":
        raise HTTPException(502, flw.get("message", "Flutterwave rejected the request"))

    payment_link = flw["data"]["link"]

    # Record pending transaction
    await db.execute(
        text("""
            INSERT INTO wallet_transactions
                (id, wallet_id, type, amount, description, reference, status, created_at)
            VALUES
                (gen_random_uuid(), CAST(:wid AS UUID), 'deposit', :amt,
                 'Wallet top-up via Flutterwave', :ref, 'pending', NOW())
        """),
        {"wid": wallet["id"], "amt": amount, "ref": tx_ref},
    )

    return {"payment_link": payment_link, "tx_ref": tx_ref}


# ══════════════════════════════════════════════════════════
# GET /wallet/flw/verify?tx_ref=xxx&transaction_id=xxx
# Called after Flutterwave redirects user back
# ══════════════════════════════════════════════════════════

@router.get("/flw/verify")
async def flw_verify(
    tx_ref:         str = "",
    transaction_id: str = "",
    status:         str = "",
    user: User = Depends(get_current_active_user),
    db:   AsyncSession = Depends(get_db),
):
    """
    Verify Flutterwave payment after redirect.
    Flutterwave appends ?tx_ref=xxx&transaction_id=xxx&status=successful
    """
    if status == "cancelled":
        raise HTTPException(400, "Payment was cancelled")

    if not transaction_id and not tx_ref:
        raise HTTPException(400, "transaction_id or tx_ref is required")

    # Idempotency check
    r = await db.execute(
        text("SELECT status FROM wallet_transactions WHERE reference = :ref"),
        {"ref": tx_ref},
    )
    tx = r.fetchone()
    if tx and tx[0] == "success":
        return {"message": "Already verified", "credited": False}

    # Verify with Flutterwave
    verify_url = f"{FLW_BASE}/transactions/{transaction_id}/verify" if transaction_id \
                 else f"{FLW_BASE}/transactions/verify_by_reference?tx_ref={tx_ref}"

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(verify_url, headers=_flw_headers())
    except Exception as e:
        raise HTTPException(502, f"Could not verify payment: {e}")

    if resp.status_code != 200:
        raise HTTPException(502, f"Flutterwave verification failed: {resp.text[:200]}")

    flw      = resp.json()
    flw_data = flw.get("data", {})

    if flw.get("status") != "success" or flw_data.get("status") != "successful":
        raise HTTPException(400, f"Payment not successful: {flw_data.get('processor_response', 'unknown')}")

    amount_naira = float(flw_data.get("amount", 0))
    wallet       = await _get_or_create_wallet(str(user.id), db)

    # Credit wallet
    await db.execute(
        text("""
            UPDATE wallets
            SET balance      = balance + :amt,
                total_earned = total_earned + :amt
            WHERE id = CAST(:wid AS UUID)
        """),
        {"amt": amount_naira, "wid": wallet["id"]},
    )

    # Upsert transaction
    updated = await db.execute(
        text("""
            UPDATE wallet_transactions
            SET status = 'success'
            WHERE reference = :ref RETURNING id
        """),
        {"ref": tx_ref},
    )
    if not updated.fetchone():
        await db.execute(
            text("""
                INSERT INTO wallet_transactions
                    (id, wallet_id, type, amount, description, reference, status, created_at)
                VALUES (gen_random_uuid(), CAST(:wid AS UUID), 'deposit', :amt,
                        'Wallet top-up via Flutterwave', :ref, 'success', NOW())
            """),
            {"wid": wallet["id"], "amt": amount_naira, "ref": tx_ref},
        )

    return {
        "message":  f"₦{amount_naira:,.2f} successfully credited to your wallet",
        "credited": True,
        "amount":   amount_naira,
    }


# ══════════════════════════════════════════════════════════
# POST /wallet/flw/webhook — Flutterwave pushes events here
# Register URL in FLW Dashboard → Settings → Webhooks
# URL: https://gfd-backend.onrender.com/api/v1/wallet/flw/webhook
# ══════════════════════════════════════════════════════════

@router.post("/flw/webhook")
async def flw_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    """Handle Flutterwave webhook events.
    Register URL in FLW Dashboard → Settings → Webhooks:
      URL:         https://gfd-backend.onrender.com/api/v1/wallet/flw/webhook
      Secret Hash: set FLW_WEBHOOK_HASH env var to match what you put in dashboard
    """
    body = await request.body()

    # ── Verify secret hash (prevents fake webhook calls) ──
    flw_hash = request.headers.get("verif-hash", "")
    if settings.FLW_WEBHOOK_HASH:
        if flw_hash != settings.FLW_WEBHOOK_HASH:
            raise HTTPException(
                status_code=401,
                detail="Invalid webhook signature"
            )

    try:
        event = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    ev_type = event.get("event", "")
    ev_data = event.get("data", {})

    if ev_type == "charge.completed" and ev_data.get("status") == "successful":
        tx_ref       = ev_data.get("tx_ref", "")
        amount_naira = float(ev_data.get("amount", 0))
        meta         = ev_data.get("meta", {}) or {}
        wallet_id    = meta.get("wallet_id")

        if not wallet_id:
            # Look up by customer email
            email = ev_data.get("customer", {}).get("email", "")
            if email:
                r2 = await db.execute(
                    text("""
                        SELECT w.id FROM wallets w
                        JOIN users u ON u.id = w.user_id
                        WHERE u.email = :email
                    """),
                    {"email": email},
                )
                row2 = r2.fetchone()
                if row2:
                    wallet_id = str(row2[0])

        if wallet_id and tx_ref and amount_naira > 0:
            r = await db.execute(
                text("SELECT status FROM wallet_transactions WHERE reference = :ref"),
                {"ref": tx_ref},
            )
            existing = r.fetchone()
            if not existing or existing[0] != "success":
                await db.execute(
                    text("""
                        UPDATE wallets
                        SET balance      = balance + :amt,
                            total_earned = total_earned + :amt
                        WHERE id = CAST(:wid AS UUID)
                    """),
                    {"amt": amount_naira, "wid": wallet_id},
                )
                if existing:
                    await db.execute(
                        text("UPDATE wallet_transactions SET status='success' WHERE reference=:ref"),
                        {"ref": tx_ref},
                    )
                else:
                    await db.execute(
                        text("""
                            INSERT INTO wallet_transactions
                                (id, wallet_id, type, amount, description, reference, status, created_at)
                            VALUES (gen_random_uuid(), CAST(:wid AS UUID), 'deposit', :amt,
                                    'Wallet top-up via Flutterwave', :ref, 'success', NOW())
                        """),
                        {"wid": wallet_id, "amt": amount_naira, "ref": tx_ref},
                    )

    elif ev_type == "transfer.completed":
        ref    = ev_data.get("reference", "")
        status = ev_data.get("status", "")
        if ref:
            await db.execute(
                text("""
                    UPDATE wallet_transactions
                    SET status = :st
                    WHERE reference = :ref AND type = 'withdrawal'
                """),
                {"st": "success" if status == "SUCCESSFUL" else "failed", "ref": ref},
            )
            # If transfer failed, refund the wallet
            if status != "SUCCESSFUL":
                amount_naira = float(ev_data.get("amount", 0))
                r3 = await db.execute(
                    text("""
                        SELECT wt.wallet_id FROM wallet_transactions wt
                        WHERE wt.reference = :ref AND wt.type = 'withdrawal'
                    """),
                    {"ref": ref},
                )
                row3 = r3.fetchone()
                if row3 and amount_naira > 0:
                    await db.execute(
                        text("""
                            UPDATE wallets
                            SET balance = balance + :amt,
                                total_withdrawn = total_withdrawn - :amt
                            WHERE id = CAST(:wid AS UUID)
                        """),
                        {"amt": amount_naira, "wid": str(row3[0])},
                    )

    return {"status": "ok"}


# ══════════════════════════════════════════════════════════
# GET /wallet/banks — Nigerian bank list
# ══════════════════════════════════════════════════════════

@router.get("/banks")
async def get_banks(user: User = Depends(get_current_active_user)):
    """Fetch live Nigerian bank list from Flutterwave."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{FLW_BASE}/banks/NG", headers=_flw_headers())
        if resp.status_code == 200:
            banks = resp.json().get("data", [])
            return {"banks": [{"name": b["name"], "code": b["code"]} for b in banks]}
    except Exception:
        pass
    # Fallback static list
    return {"banks": [
        {"name": "Access Bank",        "code": "044"},
        {"name": "GTBank",             "code": "058"},
        {"name": "First Bank",         "code": "011"},
        {"name": "Zenith Bank",        "code": "057"},
        {"name": "UBA",                "code": "033"},
        {"name": "Fidelity Bank",      "code": "070"},
        {"name": "Kuda Bank",          "code": "50211"},
        {"name": "Opay",               "code": "999992"},
        {"name": "PalmPay",            "code": "999991"},
        {"name": "Sterling Bank",      "code": "232"},
        {"name": "Wema Bank",          "code": "035"},
        {"name": "FCMB",               "code": "214"},
        {"name": "Stanbic IBTC",       "code": "221"},
        {"name": "Union Bank",         "code": "032"},
        {"name": "Ecobank",            "code": "050"},
        {"name": "Polaris Bank",       "code": "076"},
        {"name": "Keystone Bank",      "code": "082"},
        {"name": "Heritage Bank",      "code": "030"},
        {"name": "Providus Bank",      "code": "101"},
        {"name": "VFD Microfinance",   "code": "566"},
    ]}


# ══════════════════════════════════════════════════════════
# POST /wallet/verify-account — resolve account name
# ══════════════════════════════════════════════════════════

@router.post("/verify-account")
async def verify_bank_account(
    data: dict,
    user: User = Depends(get_current_active_user),
):
    account_number = data.get("account_number", "").strip()
    bank_code      = data.get("bank_code", "").strip()

    if len(account_number) != 10:
        raise HTTPException(400, "Account number must be 10 digits")
    if not bank_code:
        raise HTTPException(400, "bank_code is required")

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{FLW_BASE}/accounts/resolve",
                json={"account_number": account_number, "account_bank": bank_code},
                headers=_flw_headers(),
            )
    except Exception as e:
        raise HTTPException(502, f"Could not resolve account: {e}")

    if resp.status_code != 200:
        raise HTTPException(400, "Could not resolve account. Check account number and bank.")

    flw = resp.json()
    if flw.get("status") != "success":
        raise HTTPException(400, flw.get("message", "Account resolution failed"))

    return {
        "account_name":   flw["data"]["account_name"],
        "account_number": flw["data"]["account_number"],
    }


# ══════════════════════════════════════════════════════════
# POST /wallet/withdraw — Flutterwave Transfer
# ══════════════════════════════════════════════════════════

@router.post("/withdraw")
async def request_withdrawal(
    data: dict,
    user: User = Depends(get_current_active_user),
    db:   AsyncSession = Depends(get_db),
):
    amount         = float(data.get("amount", 0))
    bank_code      = data.get("bank_code", "").strip()
    account_number = data.get("account_number", "").strip()
    account_name   = data.get("account_name", "").strip()

    if amount < 500:
        raise HTTPException(400, "Minimum withdrawal is ₦500")
    if not bank_code or len(account_number) != 10 or not account_name:
        raise HTTPException(400, "bank_code, 10-digit account_number and account_name required")

    wallet = await _get_or_create_wallet(str(user.id), db)
    if wallet["balance"] < amount:
        raise HTTPException(400, f"Insufficient balance. Available: ₦{wallet['balance']:,.2f}")

    reference = f"wd-gfd-{uuid4().hex[:16]}"

    # Initiate Flutterwave transfer
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            tf_resp = await client.post(
                f"{FLW_BASE}/transfers",
                json={
                    "account_bank":    bank_code,
                    "account_number":  account_number,
                    "amount":          amount,
                    "currency":        "NGN",
                    "beneficiary_name": account_name,
                    "reference":       reference,
                    "narration":       f"GFD Wallet Withdrawal — {user.email}",
                    "debit_currency":  "NGN",
                },
                headers=_flw_headers(),
            )
    except Exception as e:
        raise HTTPException(502, f"Transfer initiation failed: {e}")

    tf = tf_resp.json()
    tf_status = tf.get("data", {}).get("status", "pending") if tf.get("status") == "success" else "pending"

    if tf_resp.status_code not in (200, 201) or tf.get("status") != "success":
        msg = tf.get("message", tf_resp.text[:200])
        raise HTTPException(502, f"Transfer failed: {msg}")

    # Deduct balance
    await db.execute(
        text("""
            UPDATE wallets
            SET balance         = balance - :amt,
                total_withdrawn = total_withdrawn + :amt
            WHERE id = CAST(:wid AS UUID)
        """),
        {"amt": amount, "wid": wallet["id"]},
    )

    # Record transaction
    await db.execute(
        text("""
            INSERT INTO wallet_transactions
                (id, wallet_id, type, amount, description, reference, status, created_at)
            VALUES (gen_random_uuid(), CAST(:wid AS UUID), 'withdrawal', :amt,
                    :desc, :ref, :st, NOW())
        """),
        {
            "wid":  wallet["id"],
            "amt":  amount,
            "desc": f"Withdrawal → {account_name} ({account_number})",
            "ref":  reference,
            "st":   tf_status,
        },
    )

    return {
        "message":   "Withdrawal initiated. Funds will arrive within minutes.",
        "reference": reference,
        "status":    tf_status,
    }


# ══════════════════════════════════════════════════════════
# ADMIN ENDPOINTS
# ══════════════════════════════════════════════════════════

@router.get("/admin/overview")
async def admin_overview(
    _: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    r = await db.execute(text("""
        SELECT COUNT(*), COALESCE(SUM(balance),0),
               COALESCE(SUM(total_earned),0), COALESCE(SUM(total_withdrawn),0)
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
    r = await db.execute(text("""
        SELECT wt.id, wt.reference, wt.amount, wt.description,
               wt.status, wt.created_at, u.email, u.full_name
        FROM wallet_transactions wt
        JOIN wallets w ON w.id = wt.wallet_id
        JOIN users u   ON u.id = w.user_id
        WHERE wt.type = 'withdrawal' AND wt.status = 'pending'
        ORDER BY wt.created_at ASC
    """))
    rows = r.fetchall()
    return {"withdrawals": [
        {"id": str(row[0]), "reference": row[1], "amount": float(row[2] or 0),
         "description": row[3], "status": row[4], "created_at": str(row[5]),
         "user_email": row[6], "user_name": row[7]}
        for row in rows
    ]}


@router.post("/admin/credit-user")
async def admin_credit_user(
    data: dict,
    _: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
):
    user_id     = data.get("user_id", "")
    amount      = float(data.get("amount", 0))
    description = data.get("description", "Admin credit")
    if amount <= 0:
        raise HTTPException(400, "Amount must be positive")
    wallet = await _get_or_create_wallet(user_id, db)
    await db.execute(
        text("""
            UPDATE wallets SET balance = balance + :amt, total_earned = total_earned + :amt
            WHERE id = CAST(:wid AS UUID)
        """),
        {"amt": amount, "wid": wallet["id"]},
    )
    ref = f"admin-{uuid4().hex[:12]}"
    await db.execute(
        text("""
            INSERT INTO wallet_transactions
                (id, wallet_id, type, amount, description, reference, status, created_at)
            VALUES (gen_random_uuid(), CAST(:wid AS UUID), 'earning', :amt, :desc, :ref, 'success', NOW())
        """),
        {"wid": wallet["id"], "amt": amount, "desc": description, "ref": ref},
    )
    return {"message": f"₦{amount:,.2f} credited to wallet", "reference": ref}
