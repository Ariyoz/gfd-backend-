"""
NOWPayments integration — custody-free crypto deposits.
NOWPayments holds the crypto; GFD receives webhooks and credits user accounts.
API docs: https://documenter.getpostman.com/view/7907941/S1a32n38
"""

import hashlib
import hmac
import json
import logging
from decimal import Decimal
from typing import Optional

import httpx

log = logging.getLogger("gfd.nowpayments")

# Supported coins with display metadata
SUPPORTED_COINS = {
    "usdt":  {"name": "Tether USD",    "symbol": "USDT",  "network": "TRC20",  "icon": "💵", "color": "#26A17B"},
    "usdc":  {"name": "USD Coin",      "symbol": "USDC",  "network": "ERC20",  "icon": "💙", "color": "#2775CA"},
    "btc":   {"name": "Bitcoin",       "symbol": "BTC",   "network": "Bitcoin","icon": "₿",  "color": "#F7931A"},
    "eth":   {"name": "Ethereum",      "symbol": "ETH",   "network": "ERC20",  "icon": "⟠",  "color": "#627EEA"},
    "sol":   {"name": "Solana",        "symbol": "SOL",   "network": "Solana", "icon": "◎",  "color": "#9945FF"},
}


def _get_base_url(sandbox: bool) -> str:
    return "https://api.sandbox.nowpayments.io/v1" if sandbox else "https://api.nowpayments.io/v1"


def _headers(api_key: str) -> dict:
    return {
        "x-api-key": api_key,
        "Content-Type": "application/json",
    }


async def get_deposit_address(coin: str, user_id: str, api_key: str, sandbox: bool = True) -> dict:
    """
    Get or create a deposit address for a specific coin for a user.
    Each user gets a unique deposit address per coin.
    Uses payment_id = f"{user_id}_{coin}" for idempotency.
    """
    if coin not in SUPPORTED_COINS:
        raise ValueError(f"Unsupported coin: {coin}")

    base = _get_base_url(sandbox)
    coin_meta = SUPPORTED_COINS[coin]

    # Create a payment intent — NOWPayments assigns a deposit address
    payload = {
        "price_amount": 1,          # minimum — just to get address
        "price_currency": "usd",
        "pay_currency": coin,
        "order_id": f"deposit_{user_id}_{coin}",
        "order_description": f"GFD deposit address for user {user_id[:8]}",
        "ipn_callback_url": None,   # Set via dashboard
        "is_fixed_rate": False,
        "is_fee_paid_by_user": True,
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{base}/payment",
            headers=_headers(api_key),
            json=payload,
        )

    if resp.status_code not in (200, 201):
        log.error(f"NOWPayments deposit address error: {resp.status_code} {resp.text}")
        raise ValueError(f"Could not create deposit address: {resp.text}")

    data = resp.json()
    return {
        "address": data.get("pay_address", ""),
        "coin": coin.upper(),
        "network": coin_meta["network"],
        "payment_id": data.get("payment_id", ""),
        "qr_code": f"https://chart.googleapis.com/chart?chs=200x200&cht=qr&chl={data.get('pay_address','')}",
        "min_amount": data.get("pay_amount"),
    }


async def get_payment_status(payment_id: str, api_key: str, sandbox: bool = True) -> dict:
    """Check the status of a specific payment."""
    base = _get_base_url(sandbox)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{base}/payment/{payment_id}",
            headers=_headers(api_key),
        )
    if resp.status_code != 200:
        raise ValueError(f"Could not get payment status: {resp.text}")
    return resp.json()


async def get_available_currencies(api_key: str, sandbox: bool = True) -> list:
    """Get list of available currencies from NOWPayments."""
    base = _get_base_url(sandbox)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{base}/currencies", headers=_headers(api_key))
        if resp.status_code == 200:
            return resp.json().get("currencies", [])
    except Exception:
        pass
    return list(SUPPORTED_COINS.keys())


async def get_estimated_price(amount: float, from_currency: str, to_currency: str,
                               api_key: str, sandbox: bool = True) -> dict:
    """Get estimated crypto amount for a fiat amount."""
    base = _get_base_url(sandbox)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{base}/estimate?amount={amount}&currency_from={from_currency}&currency_to={to_currency}",
                headers=_headers(api_key),
            )
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return {}


def verify_ipn_signature(payload_bytes: bytes, received_sig: str, ipn_secret: str) -> bool:
    """
    Verify NOWPayments IPN webhook signature.
    HMAC-SHA512 of sorted JSON payload.
    """
    try:
        data = json.loads(payload_bytes)
        # Sort keys and re-encode
        sorted_payload = json.dumps(data, sort_keys=True, separators=(",", ":"))
        expected = hmac.new(
            ipn_secret.encode("utf-8"),
            sorted_payload.encode("utf-8"),
            hashlib.sha512,
        ).hexdigest()
        return hmac.compare_digest(expected, received_sig)
    except Exception as e:
        log.error(f"IPN signature verification error: {e}")
        return False
