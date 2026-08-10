"""
Crypto wallet endpoints — deposit addresses, balances, transactions.
Uses NOWPayments as custody provider. GFD never holds private keys.
"""

import hashlib
import hmac
import json
import logging
import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from app.database import get_db
from app.models import User
from app.core.dependencies import get_current_active_user
from app.config import get_settings
from app.integrations.nowpayments_service import (
    SUPPORTED_COINS,
    get_deposit_address,
    verify_ipn_signature,
)

router = APIRouter()
log = logging.getLogger("gfd.crypto")


# ── Helpers ──────────────────────────────────────────────────────────────────

async def _ensure_crypto_wallet(user_id: str, db: AsyncSession) -> dict:
    """Get or create crypto wallet record for user."""
    result = await db.execute(
        text("SELECT * FROM crypto_wallets WHERE user_id = CAST(:uid AS UUID)"),
        {"uid": user_id},
    )
    row = result.mappings().first()
    if row:
        return dict(row)

    wid = str(uuid.uuid4())
    await db.execute(
        text("""
            INSERT INTO crypto_wallets (id, user_id, created_at, updated_at)
            VALUES (CAST(:id AS UUID), CAST(:uid AS UUID), NOW(), NOW())
            ON CONFLICT (user_id) DO NOTHING
        """),
        {"id": wid, "uid": user_id},
    )
    await db.commit()
    result = await db.execute(
        text("SELECT * FROM crypto_wallets WHERE user_id = CAST(:uid AS UUID)"),
        {"uid": user_id},
    )
    return dict(result.mappings().first())


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/coins")
async def list_coins():
    """List supported cryptocurrencies with metadata."""
    return {
        "coins": [
            {
                "id": coin_id,
                "name": meta["name"],
                "symbol": meta["symbol"],
                "network": meta["network"],
                "icon": meta["icon"],
                "color": meta["color"],
            }
            for coin_id, meta in SUPPORTED_COINS.items()
        ]
    }


@router.get("/balance")
async def get_crypto_balance(
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Get user's crypto balances across all supported coins."""
    try:
        rows = await db.execute(
            text("""
                SELECT coin, SUM(amount_credited) as total_credited,
                       SUM(amount_withdrawn) as total_withdrawn
                FROM crypto_transactions
                WHERE user_id = CAST(:uid AS UUID) AND status = 'confirmed'
                GROUP BY coin
            """),
            {"uid": str(user.id)},
        )
        balances_by_coin = {r["coin"]: {
            "credited":   float(r["total_credited"] or 0),
            "withdrawn":  float(r["total_withdrawn"] or 0),
            "balance":    float((r["total_credited"] or 0) - (r["total_withdrawn"] or 0)),
        } for r in rows.mappings().all()}

        result = []
        for coin_id, meta in SUPPORTED_COINS.items():
            b = balances_by_coin.get(coin_id, {})
            result.append({
                "coin":          coin_id,
                "symbol":        meta["symbol"],
                "name":          meta["name"],
                "network":       meta["network"],
                "icon":          meta["icon"],
                "color":         meta["color"],
                "balance":       b.get("balance", 0),
                "total_received": b.get("credited", 0),
                "total_sent":    b.get("withdrawn", 0),
            })

        return {"balances": result}
    except Exception as e:
        # Table may not exist yet — return zeros
        log.warning(f"Crypto balance error (table may not exist): {e}")
        return {
            "balances": [
                {
                    "coin": cid, "symbol": m["symbol"], "name": m["name"],
                    "network": m["network"], "icon": m["icon"], "color": m["color"],
                    "balance": 0, "total_received": 0, "total_sent": 0,
                }
                for cid, m in SUPPORTED_COINS.items()
            ]
        }


@router.get("/deposit-address/{coin}")
async def get_crypto_deposit_address(
    coin: str,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Get or create a deposit address for the specified coin.
    Users send crypto to this address — NOWPayments notifies us via webhook.
    """
    coin = coin.lower()
    if coin not in SUPPORTED_COINS:
        raise HTTPException(status_code=400, detail=f"Unsupported coin. Supported: {list(SUPPORTED_COINS.keys())}")

    s = get_settings()
    if not s.NOWPAYMENTS_API_KEY:
        # Return mock address for development
        mock_addresses = {
            "usdt":  "TQn9Y2khEsLJW1ChVWFMSMeRDow5KcbLSE",
            "usdc":  "0x742d35Cc6634C0532925a3b8D4C9C2E4b4b5a5a5",
            "btc":   "bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh",
            "eth":   "0x742d35Cc6634C0532925a3b8D4C9C2E4b4b5a5a5",
            "sol":   "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU",
        }
        meta = SUPPORTED_COINS[coin]
        addr = mock_addresses.get(coin, "N/A")
        return {
            "coin":       coin.upper(),
            "symbol":     meta["symbol"],
            "name":       meta["name"],
            "network":    meta["network"],
            "address":    addr,
            "qr_code":    f"https://chart.googleapis.com/chart?chs=200x200&cht=qr&chl={addr}",
            "note":       "Send only " + meta["symbol"] + " on " + meta["network"] + " network. Minimum deposit: $1 equivalent.",
            "warning":    "NOWPayments API key not configured — this is a demo address.",
        }

    try:
        result = await get_deposit_address(
            coin=coin,
            user_id=str(user.id),
            api_key=s.NOWPAYMENTS_API_KEY,
            sandbox=s.NOWPAYMENTS_SANDBOX,
        )
        meta = SUPPORTED_COINS[coin]
        return {
            "coin":       coin.upper(),
            "symbol":     meta["symbol"],
            "name":       meta["name"],
            "network":    meta["network"],
            "address":    result["address"],
            "qr_code":    result["qr_code"],
            "payment_id": result["payment_id"],
            "note":       f"Send only {meta['symbol']} on {meta['network']} network. Minimum deposit: ~$1.",
        }
    except Exception as e:
        log.error(f"Deposit address error: {e}")
        raise HTTPException(status_code=502, detail=f"Could not generate deposit address: {str(e)}")


@router.get("/transactions")
async def get_crypto_transactions(
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """Get user's crypto transaction history."""
    try:
        rows = await db.execute(
            text("""
                SELECT id, coin, type, amount_credited, amount_withdrawn, usd_value,
                       tx_hash, from_address, to_address, status, confirmations,
                       network, payment_id, created_at
                FROM crypto_transactions
                WHERE user_id = CAST(:uid AS UUID)
                ORDER BY created_at DESC
                LIMIT 50
            """),
            {"uid": str(user.id)},
        )
        txs = []
        for r in rows.mappings().all():
            meta = SUPPORTED_COINS.get(r["coin"].lower(), {})
            txs.append({
                "id":           str(r["id"]),
                "coin":         r["coin"],
                "symbol":       meta.get("symbol", r["coin"].upper()),
                "type":         r["type"],
                "amount":       float(r.get("amount_credited") or r.get("amount_withdrawn") or 0),
                "usd_value":    float(r.get("usd_value") or 0),
                "tx_hash":      r.get("tx_hash"),
                "status":       r["status"],
                "confirmations": r.get("confirmations", 0),
                "network":      r.get("network"),
                "created_at":   str(r["created_at"]) if r.get("created_at") else None,
            })
        return {"transactions": txs, "total": len(txs)}
    except Exception as e:
        log.warning(f"Crypto transactions error (table may not exist): {e}")
        return {"transactions": [], "total": 0}


# ── NOWPayments IPN Webhook ────────────────────────────────────────────────────

@router.post("/webhook/nowpayments")
async def nowpayments_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Receive IPN (Instant Payment Notification) from NOWPayments.
    Credits user crypto balance when payment is confirmed.
    Always returns 200 — NOWPayments retries on non-200.
    """
    body = await request.body()
    sig = request.headers.get("x-nowpayments-sig", "")

    s = get_settings()

    # Verify signature in production
    if s.NOWPAYMENTS_IPN_SECRET and not s.NOWPAYMENTS_SANDBOX:
        if not verify_ipn_signature(body, sig, s.NOWPAYMENTS_IPN_SECRET):
            log.warning("NOWPayments IPN: invalid signature")
            return {"status": "invalid_signature"}

    try:
        data = json.loads(body)
    except Exception:
        return {"status": "invalid_json"}

    payment_status = data.get("payment_status", "")
    order_id       = data.get("order_id", "")
    payment_id     = str(data.get("payment_id", ""))
    coin           = data.get("pay_currency", "").lower()
    amount         = Decimal(str(data.get("actually_paid", 0)))
    usd_value      = Decimal(str(data.get("price_amount", 0)))
    tx_hash        = data.get("payin_hash", "")

    log.info(f"NOWPayments IPN: status={payment_status} order={order_id} coin={coin} amount={amount}")

    # Only credit on confirmed/finished status
    if payment_status not in ("confirmed", "finished"):
        return {"status": f"ignored_{payment_status}"}

    # Extract user_id from order_id format: deposit_{user_id}_{coin}
    if not order_id.startswith("deposit_"):
        return {"status": "ignored_unknown_order"}

    parts = order_id.split("_")
    if len(parts) < 3:
        return {"status": "ignored_bad_order_id"}

    user_id = parts[1]

    # Idempotency — never double-credit
    dup = await db.execute(
        text("SELECT id FROM crypto_transactions WHERE payment_id = :pid AND status = 'confirmed'"),
        {"pid": payment_id},
    )
    if dup.mappings().first():
        log.info(f"Already processed payment_id={payment_id}")
        return {"status": "already_processed"}

    try:
        await db.execute(
            text("""
                INSERT INTO crypto_transactions
                  (id, user_id, coin, type, amount_credited, amount_withdrawn, usd_value,
                   tx_hash, payment_id, network, status, confirmations, created_at, updated_at)
                VALUES
                  (gen_random_uuid(), CAST(:uid AS UUID), :coin, 'deposit', :amount, 0, :usd,
                   :tx_hash, :pid, :network, 'confirmed', 1, NOW(), NOW())
                ON CONFLICT (payment_id) DO UPDATE
                  SET status = 'confirmed', updated_at = NOW()
            """),
            {
                "uid":     user_id,
                "coin":    coin,
                "amount":  str(amount),
                "usd":     str(usd_value),
                "tx_hash": tx_hash,
                "pid":     payment_id,
                "network": SUPPORTED_COINS.get(coin, {}).get("network", coin.upper()),
            },
        )
        await db.commit()
        log.info(f"Crypto credited: user={user_id} {amount} {coin.upper()} (${usd_value})")
    except Exception as e:
        log.error(f"Error crediting crypto: {e}")

    return {"status": "ok"}
