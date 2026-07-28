"""Wallet endpoints — balance, fund, withdraw, transactions, virtual account."""

import json
import uuid
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text, select

from app.database import get_db
from app.models import User
from app.core.dependencies import get_current_active_user
from app.schemas.wallet import (
    WalletResponse,
    TransactionListResponse,
    TransactionResponse,
    FundWalletRequest,
    FundWalletResponse,
    VerifyPaymentRequest,
    WithdrawRequest,
    WithdrawResponse,
    WithdrawalListResponse,
    VirtualAccountResponse,
)

router = APIRouter()

# ── Withdrawal fee (flat or %) ────────────────────────────────────────────────
WITHDRAWAL_FEE_FLAT = Decimal("50")   # ₦50 flat fee per withdrawal


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

async def _get_or_create_wallet(user_id: str, db: AsyncSession) -> dict:
    """Return wallet row, creating one if it doesn't exist yet."""
    result = await db.execute(
        text("SELECT * FROM wallets WHERE user_id = CAST(:uid AS UUID)"),
        {"uid": user_id},
    )
    row = result.mappings().first()
    if row:
        return dict(row)

    wallet_id = str(uuid.uuid4())
    await db.execute(
        text("""
            INSERT INTO wallets (id, user_id, balance, total_earned, total_withdrawn, total_spent, is_frozen, created_at, updated_at)
            VALUES (CAST(:id AS UUID), CAST(:uid AS UUID), 0, 0, 0, 0, FALSE, NOW(), NOW())
            ON CONFLICT (user_id) DO NOTHING
        """),
        {"id": wallet_id, "uid": user_id},
    )
    await db.commit()

    result = await db.execute(
        text("SELECT * FROM wallets WHERE user_id = CAST(:uid AS UUID)"),
        {"uid": user_id},
    )
    return dict(result.mappings().first())


async def _credit_wallet(wallet_id: str, amount: Decimal, description: str,
                         reference: str, provider: str, db: AsyncSession) -> None:
    """Credit wallet and record the transaction atomically."""
    # Lock row for update
    result = await db.execute(
        text("SELECT balance FROM wallets WHERE id = CAST(:wid AS UUID) FOR UPDATE"),
        {"wid": wallet_id},
    )
    row = result.mappings().first()
    if not row:
        raise ValueError("Wallet not found")

    balance_before = Decimal(str(row["balance"]))
    balance_after = balance_before + amount

    await db.execute(
        text("""
            UPDATE wallets
            SET balance = :bal_after,
                total_earned = total_earned + :amount,
                updated_at = NOW()
            WHERE id = CAST(:wid AS UUID)
        """),
        {"bal_after": str(balance_after), "amount": str(amount), "wid": wallet_id},
    )

    await db.execute(
        text("""
            INSERT INTO wallet_transactions
              (id, wallet_id, type, amount, fee, balance_before, balance_after,
               description, reference, provider, status, created_at, updated_at)
            VALUES
              (gen_random_uuid(), CAST(:wid AS UUID), 'credit', :amount, 0,
               :bb, :ba, :desc, :ref, :prov, 'success', NOW(), NOW())
        """),
        {
            "wid": wallet_id, "amount": str(amount),
            "bb": str(balance_before), "ba": str(balance_after),
            "desc": description, "ref": reference, "prov": provider,
        },
    )


# ══════════════════════════════════════════════════════════════════════════════
# Endpoints
# ══════════════════════════════════════════════════════════════════════════════

@router.get("", response_model=WalletResponse)
@router.get("/", response_model=WalletResponse)
async def get_wallet(
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Get current user's wallet balance and stats."""
    try:
        wallet = await _get_or_create_wallet(str(user.id), db)
        return {
            "id": str(wallet["id"]),
            "user_id": str(wallet["user_id"]),
            "balance": wallet.get("balance") or Decimal("0"),
            "total_earned": wallet.get("total_earned") or Decimal("0"),
            "total_withdrawn": wallet.get("total_withdrawn") or Decimal("0"),
            "total_spent": wallet.get("total_spent") or Decimal("0"),
            "is_frozen": wallet.get("is_frozen") or False,
            "created_at": wallet.get("created_at"),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Wallet error: {str(e)}")


@router.get("/transactions", response_model=TransactionListResponse)
async def get_transactions(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    tx_type: Optional[str] = Query(None, description="Filter by type: credit|debit|withdrawal|refund"),
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Get paginated wallet transaction history."""
    try:
        wallet = await _get_or_create_wallet(str(user.id), db)
        wallet_id = str(wallet["id"])

        offset = (page - 1) * page_size

        base_query = "FROM wallet_transactions WHERE wallet_id = CAST(:wid AS UUID)"
        params: dict = {"wid": wallet_id}

        if tx_type:
            base_query += " AND type = :type"
            params["type"] = tx_type

        count_result = await db.execute(
            text(f"SELECT COUNT(*) {base_query}"), params
        )
        total = count_result.scalar_one()

        rows_result = await db.execute(
            text(f"""
                SELECT id, wallet_id, type, amount, fee, balance_before, balance_after,
                       description, reference, provider, status, created_at
                {base_query}
                ORDER BY created_at DESC
                LIMIT :limit OFFSET :offset
            """),
            {**params, "limit": page_size, "offset": offset},
        )
        rows = rows_result.mappings().all()

        transactions = [
            {
                "id": str(r["id"]),
                "wallet_id": str(r["wallet_id"]),
                "type": r["type"],
                "amount": r["amount"],
                "fee": r.get("fee") or Decimal("0"),
                "balance_before": r.get("balance_before"),
                "balance_after": r.get("balance_after"),
                "description": r.get("description"),
                "reference": r.get("reference"),
                "provider": r.get("provider"),
                "status": r["status"],
                "created_at": r.get("created_at"),
            }
            for r in rows
        ]

        return {
            "transactions": transactions,
            "total": total,
            "page": page,
            "page_size": page_size,
            "has_more": (page * page_size) < total,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Transactions error: {str(e)}")


@router.post("/fund", response_model=FundWalletResponse)
async def fund_wallet(
    payload: FundWalletRequest,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Initiate a wallet top-up via Paystack or Flutterwave.
    Returns a payment URL the frontend should redirect to.
    """
    wallet = await _get_or_create_wallet(str(user.id), db)

    if wallet.get("is_frozen"):
        raise HTTPException(status_code=403, detail="Wallet is frozen. Contact support.")

    try:
        from app.integrations.paystack_service import initialize_payment, generate_reference
        reference = generate_reference()
        result = await initialize_payment(
            email=user.email,
            amount_naira=payload.amount,
            reference=reference,
            metadata={"user_id": str(user.id), "wallet_id": str(wallet["id"]), "type": "wallet_fund"},
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Payment provider error: {str(e)}")

    # Record a PENDING transaction so we can match the webhook/verify call
    await db.execute(
        text("""
            INSERT INTO wallet_transactions
              (id, wallet_id, type, amount, fee, description, reference, provider, status, created_at, updated_at)
            VALUES
              (gen_random_uuid(), CAST(:wid AS UUID), 'credit', :amount, 0,
               'Wallet top-up', :ref, :prov, 'pending', NOW(), NOW())
        """),
        {
            "wid": str(wallet["id"]),
            "amount": str(payload.amount),
            "ref": reference,
            "prov": "paystack",
        },
    )
    await db.commit()

    return {
        "payment_url": result["payment_url"],
        "reference": reference,
        "amount": payload.amount,
        "provider": "paystack",
    }


@router.post("/verify")
async def verify_payment(
    payload: VerifyPaymentRequest,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Verify a payment by reference and credit the wallet if successful.
    Call this after the user returns from the payment page.
    """
    # Check if already processed
    existing = await db.execute(
        text("""
            SELECT wt.id, wt.status, wt.amount, w.user_id
            FROM wallet_transactions wt
            JOIN wallets w ON w.id = wt.wallet_id
            WHERE wt.reference = :ref
        """),
        {"ref": payload.reference},
    )
    tx_row = existing.mappings().first()

    if not tx_row:
        raise HTTPException(status_code=404, detail="Transaction reference not found")

    if str(tx_row["user_id"]) != str(user.id):
        raise HTTPException(status_code=403, detail="Transaction does not belong to you")

    if tx_row["status"] == "success":
        return {"message": "Already credited", "status": "success", "amount": tx_row["amount"]}

    # Verify with provider
    if payload.provider == "paystack":
        from app.integrations.paystack_service import verify_payment as pstack_verify
        result = await pstack_verify(payload.reference)
    else:
        from app.integrations.flutterwave_service import verify_payment as flw_verify
        result = await flw_verify(payload.reference)

    if not result["success"]:
        await db.execute(
            text("UPDATE wallet_transactions SET status = 'failed', updated_at = NOW() WHERE reference = :ref"),
            {"ref": payload.reference},
        )
        await db.commit()
        raise HTTPException(status_code=402, detail="Payment not completed or failed")

    # Credit the wallet — update existing pending tx in place (avoids UNIQUE reference clash)
    wallet = await _get_or_create_wallet(str(user.id), db)
    amount = Decimal(str(result["amount_naira"]))

    # Lock wallet row and get current balance
    bal_row = await db.execute(
        text("SELECT balance FROM wallets WHERE id = CAST(:wid AS UUID) FOR UPDATE"),
        {"wid": str(wallet["id"])},
    )
    current = Decimal(str(bal_row.mappings().first()["balance"]))
    new_balance = current + amount

    # Update wallet balance
    await db.execute(
        text("""
            UPDATE wallets
            SET balance = :bal, total_earned = total_earned + :amt, updated_at = NOW()
            WHERE id = CAST(:wid AS UUID)
        """),
        {"bal": str(new_balance), "amt": str(amount), "wid": str(wallet["id"])},
    )

    # Update the pending tx to success — no new insert, avoids UNIQUE constraint on reference
    await db.execute(
        text("""
            UPDATE wallet_transactions
            SET status        = 'success',
                balance_before = :bb,
                balance_after  = :ba,
                updated_at    = NOW()
            WHERE reference = :ref AND status = 'pending'
        """),
        {"bb": str(current), "ba": str(new_balance), "ref": payload.reference},
    )
    await db.commit()

    return {
        "message": "Wallet credited successfully",
        "amount": str(amount),
        "status": "success",
    }

@router.post("/withdraw", response_model=WithdrawResponse)
async def withdraw(
    payload: WithdrawRequest,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Request a wallet withdrawal to a bank account.
    Deducts balance immediately; actual transfer processed by admin or automated job.
    """
    wallet = await _get_or_create_wallet(str(user.id), db)

    if wallet.get("is_frozen"):
        raise HTTPException(status_code=403, detail="Wallet is frozen. Contact support.")

    balance = Decimal(str(wallet["balance"]))
    fee = WITHDRAWAL_FEE_FLAT
    total_debit = payload.amount + fee

    if balance < total_debit:
        raise HTTPException(
            status_code=400,
            detail=f"Insufficient balance. Need ₦{total_debit} (amount + ₦{fee} fee), have ₦{balance}.",
        )

    net_amount = payload.amount - fee
    reference = f"WD-{uuid.uuid4().hex[:12].upper()}"
    request_id = str(uuid.uuid4())

    # Deduct balance atomically
    await db.execute(
        text("""
            UPDATE wallets
            SET balance = balance - :debit,
                total_withdrawn = total_withdrawn + :amount,
                updated_at = NOW()
            WHERE id = CAST(:wid AS UUID) AND balance >= :debit
        """),
        {"debit": str(total_debit), "amount": str(payload.amount), "wid": str(wallet["id"])},
    )

    # Record transaction
    await db.execute(
        text("""
            INSERT INTO wallet_transactions
              (id, wallet_id, type, amount, fee, balance_before, balance_after,
               description, reference, provider, status, created_at, updated_at)
            VALUES
              (gen_random_uuid(), CAST(:wid AS UUID), 'withdrawal', :amount, :fee,
               :bb, :ba, :desc, :ref, 'manual', 'pending', NOW(), NOW())
        """),
        {
            "wid": str(wallet["id"]),
            "amount": str(payload.amount),
            "fee": str(fee),
            "bb": str(balance),
            "ba": str(balance - total_debit),
            "desc": f"Withdrawal to {payload.account_number} ({payload.bank_name})",
            "ref": reference,
        },
    )

    # Create withdrawal request
    await db.execute(
        text("""
            INSERT INTO withdrawal_requests
              (id, user_id, wallet_id, amount, fee, net_amount,
               bank_name, account_name, account_number, bank_code,
               status, reference, provider, created_at, updated_at)
            VALUES
              (CAST(:id AS UUID), CAST(:uid AS UUID), CAST(:wid AS UUID),
               :amount, :fee, :net,
               :bank_name, :account_name, :account_number, :bank_code,
               'pending', :ref, 'manual', NOW(), NOW())
        """),
        {
            "id": request_id,
            "uid": str(user.id),
            "wid": str(wallet["id"]),
            "amount": str(payload.amount),
            "fee": str(fee),
            "net": str(net_amount),
            "bank_name": payload.bank_name,
            "account_name": payload.account_name,
            "account_number": payload.account_number,
            "bank_code": payload.bank_code or "",
            "ref": reference,
        },
    )
    await db.commit()

    return {
        "id": request_id,
        "amount": payload.amount,
        "fee": fee,
        "net_amount": net_amount,
        "bank_name": payload.bank_name,
        "account_name": payload.account_name,
        "account_number": payload.account_number,
        "status": "pending",
        "created_at": None,  # Will be filled by DB
    }


@router.get("/withdrawals", response_model=WithdrawalListResponse)
async def get_withdrawals(
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Get current user's withdrawal history."""
    result = await db.execute(
        text("""
            SELECT id, amount, fee, net_amount, bank_name, account_name,
                   account_number, status, created_at
            FROM withdrawal_requests
            WHERE user_id = CAST(:uid AS UUID)
            ORDER BY created_at DESC
            LIMIT 50
        """),
        {"uid": str(user.id)},
    )
    rows = result.mappings().all()
    total_result = await db.execute(
        text("SELECT COUNT(*) FROM withdrawal_requests WHERE user_id = CAST(:uid AS UUID)"),
        {"uid": str(user.id)},
    )
    total = total_result.scalar_one()

    withdrawals = [
        {
            "id": str(r["id"]),
            "amount": r["amount"],
            "fee": r["fee"],
            "net_amount": r["net_amount"],
            "bank_name": r["bank_name"],
            "account_name": r["account_name"],
            "account_number": r["account_number"],
            "status": r["status"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]
    return {"withdrawals": withdrawals, "total": total}


@router.get("/virtual-account", response_model=VirtualAccountResponse)
async def get_virtual_account(
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Get the user's dedicated virtual account for direct bank transfers."""
    result = await db.execute(
        text("""
            SELECT bank_name, account_name, account_number, provider, is_active
            FROM virtual_accounts
            WHERE user_id = CAST(:uid AS UUID)
        """),
        {"uid": str(user.id)},
    )
    row = result.mappings().first()

    if not row:
        raise HTTPException(
            status_code=404,
            detail="No virtual account yet. Use POST /wallet/virtual-account to create one.",
        )

    return dict(row)


@router.post("/virtual-account", response_model=VirtualAccountResponse)
async def create_virtual_account(
    provider: str = "paystack",
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Create a dedicated virtual account (DVA) for the user.
    Transfers to this account auto-credit their wallet via webhook.
    """
    if provider not in ("paystack", "flutterwave"):
        raise HTTPException(status_code=400, detail="Provider must be paystack or flutterwave")

    # Check if already exists
    existing = await db.execute(
        text("SELECT id FROM virtual_accounts WHERE user_id = CAST(:uid AS UUID)"),
        {"uid": str(user.id)},
    )
    if existing.mappings().first():
        raise HTTPException(status_code=409, detail="Virtual account already exists. Use GET /wallet/virtual-account.")

    try:
        if provider == "paystack":
            from app.integrations.paystack_service import create_customer, create_virtual_account as pstack_dva
            customer = await create_customer(email=user.email, full_name=user.full_name)
            dva = await pstack_dva(customer_code=customer["customer_code"])
            customer_code = customer["customer_code"]
        else:
            from app.integrations.flutterwave_service import create_virtual_account as flw_dva
            dva = await flw_dva(email=user.email, full_name=user.full_name)
            customer_code = ""
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to create virtual account: {str(e)}")

    await db.execute(
        text("""
            INSERT INTO virtual_accounts
              (id, user_id, bank_name, account_name, account_number, provider,
               customer_code, dva_id, is_active, created_at, updated_at)
            VALUES
              (gen_random_uuid(), CAST(:uid AS UUID), :bank_name, :account_name,
               :account_number, :provider, :customer_code, :dva_id, TRUE, NOW(), NOW())
            ON CONFLICT (user_id) DO UPDATE
              SET bank_name = EXCLUDED.bank_name,
                  account_name = EXCLUDED.account_name,
                  account_number = EXCLUDED.account_number,
                  provider = EXCLUDED.provider,
                  customer_code = EXCLUDED.customer_code,
                  dva_id = EXCLUDED.dva_id,
                  is_active = TRUE,
                  updated_at = NOW()
        """),
        {
            "uid": str(user.id),
            "bank_name": dva["bank_name"],
            "account_name": dva["account_name"],
            "account_number": dva["account_number"],
            "provider": provider,
            "customer_code": customer_code,
            "dva_id": dva.get("dva_id", ""),
        },
    )
    await db.commit()

    return {
        "bank_name": dva["bank_name"],
        "account_name": dva["account_name"],
        "account_number": dva["account_number"],
        "provider": provider,
        "is_active": True,
    }


@router.get("/banks")
async def get_banks(
    provider: str = Query("paystack", description="paystack | flutterwave"),
):
    """List supported banks for withdrawal."""
    try:
        from app.integrations.paystack_service import list_banks
        banks = await list_banks()
        return {"banks": banks}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not fetch banks: {str(e)}")


@router.post("/verify-account")
async def verify_bank_account(
    payload: dict,
    user: User = Depends(get_current_active_user),
):
    """Resolve account number → account name via Paystack."""
    account_number = payload.get("account_number", "")
    bank_code = payload.get("bank_code", "")
    if not account_number or not bank_code:
        raise HTTPException(status_code=400, detail="account_number and bank_code are required")
    try:
        from app.integrations.paystack_service import resolve_account
        account_name = await resolve_account(account_number, bank_code)
        return {"account_name": account_name}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# ══════════════════════════════════════════════════════════════════════════════
# Webhooks (no auth — verified by signature)
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/webhook/paystack")
async def paystack_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Handle Paystack webhook events."""
    from app.integrations.paystack_service import verify_webhook_signature

    body = await request.body()
    signature = request.headers.get("x-paystack-signature", "")

    if not verify_webhook_signature(body, signature):
        raise HTTPException(status_code=400, detail="Invalid signature")

    try:
        event = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event_type = event.get("event")
    data = event.get("data", {})

    # ── Charge success (manual payment or card) ──
    if event_type == "charge.success":
        reference = data.get("reference", "")
        amount_naira = Decimal(str(data.get("amount", 0))) / 100
        metadata = data.get("metadata", {})
        user_id = metadata.get("user_id")

        if not user_id or not reference:
            return {"status": "ignored"}

        # Avoid double-crediting
        dup = await db.execute(
            text("""
                SELECT id FROM wallet_transactions
                WHERE reference = :ref AND status = 'success' AND type = 'credit'
            """),
            {"ref": reference},
        )
        if dup.mappings().first():
            return {"status": "already_processed"}

        wallet = await _get_or_create_wallet(user_id, db)

        # Lock and update wallet balance in place
        bal_row = await db.execute(
            text("SELECT balance FROM wallets WHERE id = CAST(:wid AS UUID) FOR UPDATE"),
            {"wid": str(wallet["id"])},
        )
        current = Decimal(str(bal_row.mappings().first()["balance"]))
        new_balance = current + amount_naira

        await db.execute(
            text("""
                UPDATE wallets
                SET balance = :bal, total_earned = total_earned + :amt, updated_at = NOW()
                WHERE id = CAST(:wid AS UUID)
            """),
            {"bal": str(new_balance), "amt": str(amount_naira), "wid": str(wallet["id"])},
        )
        # Update pending tx to success — no new insert
        await db.execute(
            text("""
                UPDATE wallet_transactions
                SET status = 'success', balance_before = :bb, balance_after = :ba, updated_at = NOW()
                WHERE reference = :ref AND status = 'pending'
            """),
            {"bb": str(current), "ba": str(new_balance), "ref": reference},
        )
        await db.commit()

    # ── DVA credit (dedicated virtual account) ──
    elif event_type == "dedicatedaccount.assign.success":
        pass  # DVA setup confirmation — no action needed

    elif event_type in ("transfer.success", "transfer.failed", "transfer.reversed"):
        reference = data.get("reference", "")
        new_status = {
            "transfer.success": "success",
            "transfer.failed": "failed",
            "transfer.reversed": "reversed",
        }[event_type]

        if reference:
            await db.execute(
                text("""
                    UPDATE withdrawal_requests
                    SET status = :s, updated_at = NOW()
                    WHERE reference = :ref
                """),
                {"s": new_status, "ref": reference},
            )
            await db.execute(
                text("""
                    UPDATE wallet_transactions
                    SET status = :s, updated_at = NOW()
                    WHERE reference = :ref AND type = 'withdrawal'
                """),
                {"s": new_status, "ref": reference},
            )
            await db.commit()

    return {"status": "ok"}


@router.post("/webhook/flutterwave")
async def flutterwave_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Handle Flutterwave webhook events."""
    from app.integrations.flutterwave_service import verify_webhook_signature

    body = await request.body()
    signature = request.headers.get("verif-hash", "")

    if not verify_webhook_signature(body, signature):
        raise HTTPException(status_code=400, detail="Invalid signature")

    try:
        event = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event_type = event.get("event", "")
    data = event.get("data", {})

    if event_type == "charge.completed" and data.get("status") == "successful":
        reference = data.get("tx_ref", "")
        amount_naira = Decimal(str(data.get("amount", 0)))
        meta = data.get("meta", {})
        user_id = meta.get("user_id") if isinstance(meta, dict) else None

        if not user_id or not reference:
            return {"status": "ignored"}

        dup = await db.execute(
            text("""
                SELECT id FROM wallet_transactions
                WHERE reference = :ref AND status = 'success' AND type = 'credit'
            """),
            {"ref": reference},
        )
        if dup.mappings().first():
            return {"status": "already_processed"}

        wallet = await _get_or_create_wallet(user_id, db)
        await _credit_wallet(
            wallet_id=str(wallet["id"]),
            amount=amount_naira,
            description="Wallet top-up via Flutterwave",
            reference=reference,
            provider="flutterwave",
            db=db,
        )
        await db.execute(
            text("""
                UPDATE wallet_transactions
                SET status = 'success', updated_at = NOW()
                WHERE reference = :ref AND status = 'pending'
            """),
            {"ref": reference},
        )
        await db.commit()

    elif event_type == "transfer.completed":
        reference = data.get("reference", "")
        flw_status = data.get("status", "")
        new_status = "success" if flw_status == "SUCCESSFUL" else "failed"
        if reference:
            await db.execute(
                text("""
                    UPDATE withdrawal_requests SET status = :s, updated_at = NOW()
                    WHERE reference = :ref
                """),
                {"s": new_status, "ref": reference},
            )
            await db.execute(
                text("""
                    UPDATE wallet_transactions SET status = :s, updated_at = NOW()
                    WHERE reference = :ref AND type = 'withdrawal'
                """),
                {"s": new_status, "ref": reference},
            )
            await db.commit()

    return {"status": "ok"}


# ══════════════════════════════════════════════════════════════════════════════
# SEND MONEY — user-to-user transfer
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/send")
async def send_money(
    payload: dict,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Send money to another GFD user by username or email.
    Atomic: debit sender + credit recipient in one transaction.
    """
    try:
        recipient_id_or_username = payload.get("recipient")  # username or email
        amount = Decimal(str(payload.get("amount", 0)))
        note = payload.get("note", "")

        if not recipient_id_or_username:
            raise HTTPException(400, "recipient is required (username or email)")
        if amount < Decimal("100"):
            raise HTTPException(400, "Minimum transfer amount is ₦100")

        # Find recipient
        r = await db.execute(
            text("SELECT * FROM users WHERE username = :q OR email = :q"),
            {"q": recipient_id_or_username},
        )
        recipient = r.mappings().first()
        if not recipient:
            raise HTTPException(404, f"User '{recipient_id_or_username}' not found")
        if str(recipient["id"]) == str(user.id):
            raise HTTPException(400, "You cannot send money to yourself")

        # Get sender wallet
        sender_wallet = await _get_or_create_wallet(str(user.id), db)
        if sender_wallet.get("is_frozen"):
            raise HTTPException(403, "Your wallet is frozen. Contact support.")

        sender_balance = Decimal(str(sender_wallet["balance"]))
        if sender_balance < amount:
            raise HTTPException(400, f"Insufficient balance. Have ₦{sender_balance:,.2f}, need ₦{amount:,.2f}")

        # Get recipient wallet
        recipient_wallet = await _get_or_create_wallet(str(recipient["id"]), db)

        ref = f"TRF-{uuid.uuid4().hex[:14].upper()}"

        # Debit sender (FOR UPDATE locks)
        s_bal_row = await db.execute(
            text("SELECT balance FROM wallets WHERE id = CAST(:wid AS UUID) FOR UPDATE"),
            {"wid": str(sender_wallet["id"])},
        )
        s_balance = Decimal(str(s_bal_row.mappings().first()["balance"]))
        if s_balance < amount:
            raise HTTPException(400, "Insufficient balance")
        s_new = s_balance - amount

        await db.execute(
            text("""
                UPDATE wallets SET balance = :bal, total_spent = total_spent + :amt, updated_at = NOW()
                WHERE id = CAST(:wid AS UUID)
            """),
            {"bal": str(s_new), "amt": str(amount), "wid": str(sender_wallet["id"])},
        )
        await db.execute(
            text("""
                INSERT INTO wallet_transactions
                  (id, wallet_id, type, amount, fee, balance_before, balance_after,
                   description, reference, provider, status, created_at, updated_at)
                VALUES (gen_random_uuid(), CAST(:wid AS UUID), 'debit', :amt, 0,
                        :bb, :ba, :desc, :ref, 'internal', 'success', NOW(), NOW())
            """),
            {
                "wid": str(sender_wallet["id"]), "amt": str(amount),
                "bb": str(s_balance), "ba": str(s_new),
                "desc": f"Sent to @{recipient['username']}" + (f" — {note}" if note else ""),
                "ref": ref,
            },
        )

        # Credit recipient
        r_bal_row = await db.execute(
            text("SELECT balance FROM wallets WHERE id = CAST(:wid AS UUID) FOR UPDATE"),
            {"wid": str(recipient_wallet["id"])},
        )
        r_balance = Decimal(str(r_bal_row.mappings().first()["balance"]))
        r_new = r_balance + amount

        await db.execute(
            text("""
                UPDATE wallets SET balance = :bal, total_earned = total_earned + :amt, updated_at = NOW()
                WHERE id = CAST(:wid AS UUID)
            """),
            {"bal": str(r_new), "amt": str(amount), "wid": str(recipient_wallet["id"])},
        )
        await db.execute(
            text("""
                INSERT INTO wallet_transactions
                  (id, wallet_id, type, amount, fee, balance_before, balance_after,
                   description, reference, provider, status, created_at, updated_at)
                VALUES (gen_random_uuid(), CAST(:wid AS UUID), 'credit', :amt, 0,
                        :bb, :ba, :desc, :ref_in, 'internal', 'success', NOW(), NOW())
            """),
            {
                "wid": str(recipient_wallet["id"]), "amt": str(amount),
                "bb": str(r_balance), "ba": str(r_new),
                "desc": f"Received from @{user.username}" + (f" — {note}" if note else ""),
                "ref_in": f"{ref}-IN",
            },
        )

        # Notify recipient
        await db.execute(
            text("""
                INSERT INTO notifications (id, user_id, actor_id, type, title, body, action_url, created_at)
                VALUES (gen_random_uuid(), CAST(:uid AS UUID), CAST(:actor AS UUID),
                        'transfer_received', :title, :body, '/wallet', NOW())
            """),
            {
                "uid": str(recipient["id"]), "actor": str(user.id),
                "title": f"₦{amount:,.0f} received from @{user.username}",
                "body": note or f"Money transfer from {user.full_name}",
            },
        )

        await db.commit()

        return {
            "message": f"₦{amount:,.2f} sent to @{recipient['username']} successfully",
            "reference": ref,
            "amount": str(amount),
            "recipient": {
                "username": recipient["username"],
                "full_name": recipient["full_name"],
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Transfer failed: {str(e)}")


# ══════════════════════════════════════════════════════════════════════════════
# REQUEST MONEY — send a payment request to another user
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/request")
async def request_money(
    payload: dict,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a money request to another GFD user."""
    try:
        recipient_username = payload.get("recipient")
        amount = Decimal(str(payload.get("amount", 0)))
        note = payload.get("note", "")

        if not recipient_username:
            raise HTTPException(400, "recipient is required")
        if amount < Decimal("100"):
            raise HTTPException(400, "Minimum request amount is ₦100")

        r = await db.execute(
            text("SELECT * FROM users WHERE username = :q OR email = :q"),
            {"q": recipient_username},
        )
        recipient = r.mappings().first()
        if not recipient:
            raise HTTPException(404, f"User '{recipient_username}' not found")
        if str(recipient["id"]) == str(user.id):
            raise HTTPException(400, "Cannot request money from yourself")

        req_id = str(uuid.uuid4())
        await db.execute(
            text("""
                INSERT INTO money_requests
                  (id, requester_id, payer_id, amount, note, status, created_at)
                VALUES (CAST(:id AS UUID), CAST(:req AS UUID), CAST(:pay AS UUID), :amt, :note, 'pending', NOW())
            """),
            {
                "id": req_id, "req": str(user.id),
                "pay": str(recipient["id"]), "amt": str(amount), "note": note,
            },
        )

        # Notify recipient
        await db.execute(
            text("""
                INSERT INTO notifications (id, user_id, actor_id, type, title, body, action_url, created_at)
                VALUES (gen_random_uuid(), CAST(:uid AS UUID), CAST(:actor AS UUID),
                        'money_request', :title, :body, '/wallet', NOW())
            """),
            {
                "uid": str(recipient["id"]), "actor": str(user.id),
                "title": f"@{user.username} is requesting ₦{amount:,.0f}",
                "body": note or f"Payment request from {user.full_name}",
            },
        )

        await db.commit()
        return {"message": "Request sent", "request_id": req_id, "amount": str(amount)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Request failed: {str(e)}")


@router.get("/requests")
async def get_money_requests(
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Get incoming and outgoing money requests."""
    try:
        r = await db.execute(
            text("""
                SELECT mr.*, 
                       req.username AS requester_username, req.full_name AS requester_name, req.avatar AS requester_avatar,
                       pay.username AS payer_username, pay.full_name AS payer_name
                FROM money_requests mr
                JOIN users req ON req.id = mr.requester_id
                JOIN users pay ON pay.id = mr.payer_id
                WHERE mr.requester_id = CAST(:uid AS UUID) OR mr.payer_id = CAST(:uid AS UUID)
                ORDER BY mr.created_at DESC LIMIT 50
            """),
            {"uid": str(user.id)},
        )
        rows = r.mappings().all()
        return {
            "requests": [
                {
                    "id": str(row["id"]),
                    "amount": str(row["amount"]),
                    "note": row.get("note"),
                    "status": row["status"],
                    "is_incoming": str(row["payer_id"]) == str(user.id),
                    "requester": {"username": row["requester_username"], "full_name": row["requester_name"], "avatar": row.get("requester_avatar")},
                    "payer": {"username": row["payer_username"], "full_name": row["payer_name"]},
                    "created_at": str(row["created_at"]),
                }
                for row in rows
            ]
        }
    except Exception as e:
        raise HTTPException(500, f"Error fetching requests: {str(e)}")


@router.post("/requests/{request_id}/accept")
async def accept_money_request(
    request_id: str,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Accept a money request — transfers funds from payer to requester."""
    try:
        r = await db.execute(
            text("SELECT * FROM money_requests WHERE id = CAST(:id AS UUID)"),
            {"id": request_id},
        )
        req = r.mappings().first()
        if not req:
            raise HTTPException(404, "Request not found")
        if str(req["payer_id"]) != str(user.id):
            raise HTTPException(403, "This request is not for you")
        if req["status"] != "pending":
            raise HTTPException(400, f"Request is already {req['status']}")

        amount = Decimal(str(req["amount"]))

        # Get wallets
        payer_wallet = await _get_or_create_wallet(str(user.id), db)
        requester_wallet = await _get_or_create_wallet(str(req["requester_id"]), db)

        payer_balance = Decimal(str(payer_wallet["balance"]))
        if payer_balance < amount:
            raise HTTPException(400, f"Insufficient balance. Have ₦{payer_balance:,.2f}, need ₦{amount:,.2f}")

        ref = f"REQ-{uuid.uuid4().hex[:12].upper()}"

        # Debit payer
        p_bal_row = await db.execute(
            text("SELECT balance FROM wallets WHERE id = CAST(:wid AS UUID) FOR UPDATE"),
            {"wid": str(payer_wallet["id"])},
        )
        p_bal = Decimal(str(p_bal_row.mappings().first()["balance"]))
        p_new = p_bal - amount

        await db.execute(
            text("UPDATE wallets SET balance = :b, total_spent = total_spent + :a, updated_at = NOW() WHERE id = CAST(:wid AS UUID)"),
            {"b": str(p_new), "a": str(amount), "wid": str(payer_wallet["id"])},
        )
        await db.execute(
            text("""
                INSERT INTO wallet_transactions (id, wallet_id, type, amount, fee, balance_before, balance_after, description, reference, provider, status, created_at, updated_at)
                VALUES (gen_random_uuid(), CAST(:wid AS UUID), 'debit', :a, 0, :bb, :ba, :desc, :ref, 'internal', 'success', NOW(), NOW())
            """),
            {"wid": str(payer_wallet["id"]), "a": str(amount), "bb": str(p_bal), "ba": str(p_new),
             "desc": f"Request paid to @{req['requester_id']}" + (f" — {req.get('note','')}" if req.get('note') else ""),
             "ref": ref},
        )

        # Credit requester
        rq_bal_row = await db.execute(
            text("SELECT balance FROM wallets WHERE id = CAST(:wid AS UUID) FOR UPDATE"),
            {"wid": str(requester_wallet["id"])},
        )
        rq_bal = Decimal(str(rq_bal_row.mappings().first()["balance"]))
        rq_new = rq_bal + amount

        await db.execute(
            text("UPDATE wallets SET balance = :b, total_earned = total_earned + :a, updated_at = NOW() WHERE id = CAST(:wid AS UUID)"),
            {"b": str(rq_new), "a": str(amount), "wid": str(requester_wallet["id"])},
        )
        await db.execute(
            text("""
                INSERT INTO wallet_transactions (id, wallet_id, type, amount, fee, balance_before, balance_after, description, reference, provider, status, created_at, updated_at)
                VALUES (gen_random_uuid(), CAST(:wid AS UUID), 'credit', :a, 0, :bb, :ba, :desc, :ref, 'internal', 'success', NOW(), NOW())
            """),
            {"wid": str(requester_wallet["id"]), "a": str(amount), "bb": str(rq_bal), "ba": str(rq_new),
             "desc": f"Request accepted by @{user.username}", "ref": f"{ref}-IN"},
        )

        # Update request status
        await db.execute(
            text("UPDATE money_requests SET status = 'accepted' WHERE id = CAST(:id AS UUID)"),
            {"id": request_id},
        )

        # Notify requester
        await db.execute(
            text("""
                INSERT INTO notifications (id, user_id, actor_id, type, title, body, action_url, created_at)
                VALUES (gen_random_uuid(), CAST(:uid AS UUID), CAST(:actor AS UUID), 'request_accepted',
                        :title, :body, '/wallet', NOW())
            """),
            {"uid": str(req["requester_id"]), "actor": str(user.id),
             "title": f"@{user.username} accepted your payment request",
             "body": f"₦{amount:,.0f} has been added to your wallet"},
        )

        await db.commit()
        return {"message": f"₦{amount:,.2f} transferred successfully", "reference": ref}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Accept failed: {str(e)}")


@router.post("/requests/{request_id}/reject")
async def reject_money_request(
    request_id: str,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Reject a money request."""
    try:
        r = await db.execute(
            text("SELECT * FROM money_requests WHERE id = CAST(:id AS UUID)"),
            {"id": request_id},
        )
        req = r.mappings().first()
        if not req:
            raise HTTPException(404, "Request not found")
        if str(req["payer_id"]) != str(user.id):
            raise HTTPException(403, "This request is not for you")
        if req["status"] != "pending":
            raise HTTPException(400, f"Request is already {req['status']}")

        await db.execute(
            text("UPDATE money_requests SET status = 'rejected' WHERE id = CAST(:id AS UUID)"),
            {"id": request_id},
        )

        # Notify requester
        await db.execute(
            text("""
                INSERT INTO notifications (id, user_id, actor_id, type, title, body, action_url, created_at)
                VALUES (gen_random_uuid(), CAST(:uid AS UUID), CAST(:actor AS UUID), 'request_rejected',
                        :title, :body, '/wallet', NOW())
            """),
            {"uid": str(req["requester_id"]), "actor": str(user.id),
             "title": f"@{user.username} declined your payment request",
             "body": f"Your request for ₦{req['amount']:,.0f} was declined"},
        )

        await db.commit()
        return {"message": "Request rejected"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Reject failed: {str(e)}")
