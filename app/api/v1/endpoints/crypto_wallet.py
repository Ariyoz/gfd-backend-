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
from app.core.dependencies import get_current_active_user, require_admin
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
        # GFD platform mainnet addresses — user sends here, admin credits manually
        # Replace these with your actual GFD treasury/hot wallet addresses
        MAINNET_ADDRESSES = {
            "usdt":  {"address": "TQn9Y2khEsLJW1ChVWFMSMeRDow5KcbLSE",  "network": "TRC20"},
            "usdc":  {"address": "0x4838B106FCe9647Bdf1E7877BF73cE8B0BAD5f97", "network": "ERC20"},
            "btc":   {"address": "bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh", "network": "Bitcoin"},
            "eth":   {"address": "0x4838B106FCe9647Bdf1E7877BF73cE8B0BAD5f97", "network": "ERC20"},
            "sol":   {"address": "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU", "network": "Solana"},
        }
        info = MAINNET_ADDRESSES[coin]
        addr = info["address"]
        meta = SUPPORTED_COINS[coin]
        qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=200x200&data={addr}&bgcolor=ffffff&color=000000&margin=10"
        return {
            "coin":    coin.upper(),
            "symbol":  meta["symbol"],
            "name":    meta["name"],
            "network": info["network"],
            "address": addr,
            "qr_code": qr_url,
            "note":    f"Send only {meta['symbol']} on {info['network']} network. Minimum deposit: ~$1.",
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


# ── Live Prices (CoinGecko — no API key needed for free tier) ─────────────────

@router.get("/prices")
async def get_crypto_prices():
    """
    Fetch live USD prices from CoinGecko — cached 60s to avoid rate limits.
    Falls back to static prices if CoinGecko is unavailable.
    """
    import httpx
    from app.services.cache import CacheService

    CACHE_KEY = "crypto:prices:v1"
    cached = await CacheService.get(CACHE_KEY)
    if cached:
        return cached

    FALLBACK = {"btc": 67000, "eth": 3500, "sol": 145, "usdt": 1.0, "usdc": 1.0}
    COINGECKO_IDS = {
        "btc":  "bitcoin",
        "eth":  "ethereum",
        "sol":  "solana",
        "usdt": "tether",
        "usdc": "usd-coin",
    }
    try:
        ids = ",".join(COINGECKO_IDS.values())
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(
                f"https://api.coingecko.com/api/v3/simple/price"
                f"?ids={ids}&vs_currencies=usd&include_24hr_change=true",
                headers={"User-Agent": "GFD-App/1.0"},
            )
        if resp.status_code == 200:
            data = resp.json()
            prices = {}
            for coin, cg_id in COINGECKO_IDS.items():
                entry = data.get(cg_id, {})
                prices[coin] = {
                    "usd":        entry.get("usd", FALLBACK[coin]),
                    "change_24h": round(entry.get("usd_24h_change", 0), 2),
                }
            result = {"prices": prices, "source": "coingecko"}
            await CacheService.set(CACHE_KEY, result, ttl=60)
            return result
    except Exception as e:
        log.warning(f"CoinGecko unavailable: {e}")

    fallback = {
        "prices": {k: {"usd": v, "change_24h": 0} for k, v in FALLBACK.items()},
        "source": "fallback",
    }
    await CacheService.set(CACHE_KEY, fallback, ttl=30)
    return fallback


# ── Crypto Send (withdraw to external address) ────────────────────────────────

# Address format validators
import re as _re
_ADDR_VALIDATORS = {
    "btc":  _re.compile(r"^(bc1|[13])[a-zA-HJ-NP-Z0-9]{25,62}$"),
    "eth":  _re.compile(r"^0x[a-fA-F0-9]{40}$"),
    "usdc": _re.compile(r"^0x[a-fA-F0-9]{40}$"),
    "sol":  _re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$"),
    "usdt": _re.compile(r"^(T[A-Za-z1-9]{33}|0x[a-fA-F0-9]{40})$"),  # TRC20 or ERC20
}

# Max single-send limits (safety cap)
MAX_SEND = {
    "btc":  Decimal("1"),
    "eth":  Decimal("10"),
    "sol":  Decimal("1000"),
    "usdt": Decimal("50000"),
    "usdc": Decimal("50000"),
}

MIN_SEND = {
    "btc":  Decimal("0.00001"),
    "eth":  Decimal("0.0001"),
    "sol":  Decimal("0.01"),
    "usdt": Decimal("1"),
    "usdc": Decimal("1"),
}

SEND_FEES = {
    "btc":  Decimal("0.00005"),
    "eth":  Decimal("0.001"),
    "sol":  Decimal("0.001"),
    "usdt": Decimal("2"),
    "usdc": Decimal("2"),
}


@router.post("/send")
async def send_crypto(
    payload: dict,
    user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Queue a crypto send. Requires a valid PIN token in payload.
    """
    # ── PIN gate ──
    from app.api.v1.endpoints.pin_kyc import require_pin_token
    pin_token = (payload.get("pin_token") or "").strip()
    require_pin_token(pin_token, str(user.id))
    coin         = (payload.get("coin") or "").lower().strip()
    to_address   = (payload.get("to_address") or "").strip()
    network      = (payload.get("network") or "").strip()
    idempotency  = (payload.get("idempotency_key") or "").strip()

    try:
        amount = Decimal(str(payload.get("amount") or 0))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid amount")

    # ── Validate coin ──
    if coin not in SUPPORTED_COINS:
        raise HTTPException(status_code=400, detail=f"Unsupported coin. Use: {list(SUPPORTED_COINS.keys())}")

    meta = SUPPORTED_COINS[coin]

    # ── Validate address format ──
    if not to_address:
        raise HTTPException(status_code=400, detail="Destination address is required")
    validator = _ADDR_VALIDATORS.get(coin)
    if validator and not validator.match(to_address):
        raise HTTPException(status_code=400, detail=f"Invalid {meta['symbol']} address format for {network or meta['network']} network")

    # ── Validate amount ──
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")
    if amount < MIN_SEND[coin]:
        raise HTTPException(status_code=400, detail=f"Minimum send is {MIN_SEND[coin]} {meta['symbol']}")
    if amount > MAX_SEND[coin]:
        raise HTTPException(status_code=400, detail=f"Maximum single send is {MAX_SEND[coin]} {meta['symbol']}. Contact support for larger amounts.")

    # ── Idempotency check ──
    if idempotency:
        dup = await db.execute(
            text("SELECT id FROM crypto_transactions WHERE payment_id = :ikey AND user_id = CAST(:uid AS UUID)"),
            {"ikey": f"send_{idempotency}", "uid": str(user.id)},
        )
        if dup.mappings().first():
            raise HTTPException(status_code=409, detail="Duplicate send request. This transaction was already submitted.")

    fee = SEND_FEES[coin]

    # ── Check balance with row lock ──
    async with db.begin_nested():
        bal_row = await db.execute(
            text("""
                SELECT
                    COALESCE(SUM(amount_credited), 0) - COALESCE(SUM(amount_withdrawn), 0)
                    AS balance
                FROM crypto_transactions
                WHERE user_id = CAST(:uid AS UUID)
                  AND coin    = :coin
                  AND status  = 'confirmed'
            """),
            {"uid": str(user.id), "coin": coin},
        )
        bal = Decimal(str(bal_row.mappings().first()["balance"] or 0))

        total_debit = amount + fee
        if bal < total_debit:
            raise HTTPException(
                status_code=400,
                detail=f"Insufficient {meta['symbol']} balance. "
                       f"Need {float(total_debit):.8f} (including {float(fee)} fee), "
                       f"have {float(bal):.8f}",
            )

        tx_id = str(uuid.uuid4())
        ref   = f"SEND-{uuid.uuid4().hex[:12].upper()}"

        await db.execute(
            text("""
                INSERT INTO crypto_transactions
                  (id, user_id, coin, type, amount_credited, amount_withdrawn, usd_value,
                   to_address, network, status, payment_id, confirmations, created_at, updated_at)
                VALUES
                  (CAST(:id AS UUID), CAST(:uid AS UUID), :coin, 'withdrawal',
                   0, :total_debit, 0,
                   :to_addr, :network, 'pending',
                   :pid, 0, NOW(), NOW())
            """),
            {
                "id":          tx_id,
                "uid":         str(user.id),
                "coin":        coin,
                "total_debit": str(total_debit),
                "to_addr":     to_address,
                "network":     network or meta["network"],
                "pid":         f"send_{idempotency}" if idempotency else ref,
            },
        )

    await db.commit()
    log.info(
        f"CRYPTO_SEND_QUEUED user={user.id} coin={coin.upper()} "
        f"amount={amount} fee={fee} to={to_address[:16]}… ref={ref}"
    )

    return {
        "id":         tx_id,
        "reference":  ref,
        "coin":       coin.upper(),
        "symbol":     meta["symbol"],
        "amount":     float(amount),
        "fee":        float(fee),
        "total":      float(total_debit),
        "to_address": to_address,
        "network":    network or meta["network"],
        "status":     "pending",
        "message":    (
            f"Your {meta['symbol']} send of {float(amount)} is queued. "
            "It will be processed and broadcast to the network within 30 minutes."
        ),
    }


# ── Admin: Crypto overview ────────────────────────────────────────────────────

@router.get("/admin/overview")
async def admin_crypto_overview(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    try:
        # Total per coin
        rows = await db.execute(text("""
            SELECT coin,
                   COALESCE(SUM(amount_credited), 0)  AS total_received,
                   COALESCE(SUM(amount_withdrawn), 0) AS total_sent,
                   COUNT(*) FILTER (WHERE type = 'deposit')    AS deposit_count,
                   COUNT(*) FILTER (WHERE type = 'withdrawal') AS withdrawal_count
            FROM crypto_transactions
            WHERE status = 'confirmed'
            GROUP BY coin
        """))
        by_coin = {}
        for r in rows.mappings().all():
            by_coin[r["coin"]] = {
                "coin":              r["coin"],
                "symbol":            SUPPORTED_COINS.get(r["coin"], {}).get("symbol", r["coin"].upper()),
                "total_received":    float(r["total_received"] or 0),
                "total_sent":        float(r["total_sent"] or 0),
                "net_balance":       float((r["total_received"] or 0) - (r["total_sent"] or 0)),
                "deposit_count":     r["deposit_count"],
                "withdrawal_count":  r["withdrawal_count"],
            }

        # User count with any crypto activity
        user_count = await db.execute(text(
            "SELECT COUNT(DISTINCT user_id) FROM crypto_transactions WHERE status = 'confirmed'"
        ))
        total_users = user_count.scalar() or 0

        # Recent 20 transactions
        recent = await db.execute(text("""
            SELECT ct.id, ct.user_id, ct.coin, ct.type,
                   ct.amount_credited, ct.amount_withdrawn, ct.usd_value,
                   ct.tx_hash, ct.to_address, ct.status, ct.created_at,
                   u.full_name AS user_name, u.email AS user_email
            FROM crypto_transactions ct
            JOIN users u ON u.id = ct.user_id
            ORDER BY ct.created_at DESC
            LIMIT 20
        """))
        recent_txs = []
        for r in recent.mappings().all():
            recent_txs.append({
                "id":         str(r["id"]),
                "user_id":    str(r["user_id"]),
                "user_name":  r["user_name"],
                "user_email": r["user_email"],
                "coin":       r["coin"],
                "symbol":     SUPPORTED_COINS.get(r["coin"], {}).get("symbol", r["coin"].upper()),
                "type":       r["type"],
                "amount":     float(r["amount_credited"] or r["amount_withdrawn"] or 0),
                "usd_value":  float(r["usd_value"] or 0),
                "tx_hash":    r.get("tx_hash"),
                "to_address": r.get("to_address"),
                "status":     r["status"],
                "created_at": str(r["created_at"]),
            })

        # Pending sends
        pending = await db.execute(text(
            "SELECT COUNT(*) FROM crypto_transactions WHERE type = 'withdrawal' AND status = 'pending'"
        ))
        pending_sends = pending.scalar() or 0

        return {
            "by_coin":      list(by_coin.values()),
            "total_users":  total_users,
            "recent_txs":   recent_txs,
            "pending_sends": pending_sends,
        }
    except Exception as e:
        log.warning(f"Admin crypto overview error: {e}")
        return {"by_coin": [], "total_users": 0, "recent_txs": [], "pending_sends": 0}


@router.get("/admin/transactions")
async def admin_crypto_transactions(
    page: int = 1,
    page_size: int = 50,
    coin: str = None,
    tx_type: str = None,
    status: str = None,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Admin — paginated crypto transactions with filters."""
    try:
        where = ["1=1"]
        params: dict = {"limit": page_size, "offset": (page - 1) * page_size}
        if coin:
            where.append("ct.coin = :coin"); params["coin"] = coin.lower()
        if tx_type:
            where.append("ct.type = :type"); params["type"] = tx_type
        if status:
            where.append("ct.status = :status"); params["status"] = status

        where_str = " AND ".join(where)

        count_row = await db.execute(
            text(f"SELECT COUNT(*) FROM crypto_transactions ct WHERE {where_str}"), params
        )
        total = count_row.scalar() or 0

        rows = await db.execute(text(f"""
            SELECT ct.id, ct.user_id, ct.coin, ct.type,
                   ct.amount_credited, ct.amount_withdrawn, ct.usd_value,
                   ct.tx_hash, ct.to_address, ct.from_address, ct.status,
                   ct.payment_id, ct.network, ct.created_at,
                   u.full_name AS user_name, u.email AS user_email, u.avatar AS user_avatar
            FROM crypto_transactions ct
            JOIN users u ON u.id = ct.user_id
            WHERE {where_str}
            ORDER BY ct.created_at DESC
            LIMIT :limit OFFSET :offset
        """), params)

        txs = []
        for r in rows.mappings().all():
            txs.append({
                "id":          str(r["id"]),
                "user_id":     str(r["user_id"]),
                "user_name":   r["user_name"],
                "user_email":  r["user_email"],
                "user_avatar": r.get("user_avatar"),
                "coin":        r["coin"],
                "symbol":      SUPPORTED_COINS.get(r["coin"], {}).get("symbol", r["coin"].upper()),
                "type":        r["type"],
                "amount":      float(r["amount_credited"] or r["amount_withdrawn"] or 0),
                "usd_value":   float(r["usd_value"] or 0),
                "tx_hash":     r.get("tx_hash"),
                "to_address":  r.get("to_address"),
                "status":      r["status"],
                "network":     r.get("network"),
                "payment_id":  r.get("payment_id"),
                "created_at":  str(r["created_at"]),
            })

        return {"transactions": txs, "total": total, "page": page, "page_size": page_size}
    except Exception as e:
        log.warning(f"Admin crypto transactions error: {e}")
        return {"transactions": [], "total": 0, "page": page, "page_size": page_size}


@router.patch("/admin/transactions/{tx_id}/complete")
async def admin_complete_send(
    tx_id: str,
    payload: dict = {},
    db: AsyncSession = Depends(get_db),
):
    """Admin — mark a pending crypto send as completed (after manual on-chain transfer)."""
    tx_hash = payload.get("tx_hash", "")
    try:
        result = await db.execute(text("""
            UPDATE crypto_transactions
            SET status = 'confirmed', tx_hash = :tx_hash, updated_at = NOW()
            WHERE id = CAST(:tid AS UUID) AND type = 'withdrawal' AND status = 'pending'
            RETURNING id
        """), {"tid": tx_id, "tx_hash": tx_hash})
        if not result.fetchone():
            raise HTTPException(status_code=404, detail="Transaction not found or already processed")
        await db.commit()
        return {"message": "Send marked as completed", "tx_hash": tx_hash}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
