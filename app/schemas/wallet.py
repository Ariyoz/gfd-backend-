"""Wallet Pydantic schemas."""

from __future__ import annotations
from decimal import Decimal
from typing import Optional, List
from datetime import datetime
from pydantic import BaseModel, field_validator


# ── Wallet ──────────────────────────────────────────────────────────────────

class WalletResponse(BaseModel):
    id: str
    user_id: str
    balance: Decimal
    total_earned: Decimal
    total_withdrawn: Decimal
    total_spent: Decimal
    is_frozen: bool
    created_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


# ── Transactions ─────────────────────────────────────────────────────────────

class TransactionResponse(BaseModel):
    id: str
    wallet_id: str
    type: str
    amount: Decimal
    fee: Decimal
    balance_before: Optional[Decimal] = None
    balance_after: Optional[Decimal] = None
    description: Optional[str] = None
    reference: Optional[str] = None
    provider: Optional[str] = None
    status: str
    created_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class TransactionListResponse(BaseModel):
    transactions: List[TransactionResponse]
    total: int
    page: int
    page_size: int
    has_more: bool


# ── Fund wallet ──────────────────────────────────────────────────────────────

class FundWalletRequest(BaseModel):
    amount: Decimal
    provider: str = "paystack"

    @field_validator("amount")
    @classmethod
    def amount_must_be_positive(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("Amount must be greater than zero")
        if v < Decimal("100"):
            raise ValueError("Minimum funding amount is ₦100")
        return v


class FundWalletResponse(BaseModel):
    payment_url: str
    reference: str
    amount: Decimal
    provider: str


class VerifyPaymentRequest(BaseModel):
    reference: str
    provider: str = "paystack"


# ── Withdraw ─────────────────────────────────────────────────────────────────

class WithdrawRequest(BaseModel):
    amount: Decimal
    bank_name: str
    account_name: str
    account_number: str
    bank_code: Optional[str] = None
    pin_token: Optional[str] = None   # Required for actual submission

    @field_validator("amount")
    @classmethod
    def amount_must_be_positive(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("Amount must be greater than zero")
        if v < Decimal("100"):
            raise ValueError("Minimum withdrawal amount is ₦100")
        return v


class WithdrawResponse(BaseModel):
    id: str
    amount: Decimal
    fee: Decimal
    net_amount: Decimal
    bank_name: str
    account_name: str
    account_number: str
    status: str
    created_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class WithdrawalListResponse(BaseModel):
    withdrawals: List[WithdrawResponse]
    total: int


# ── Virtual Account ───────────────────────────────────────────────────────────

class VirtualAccountResponse(BaseModel):
    bank_name: Optional[str]
    account_name: Optional[str]
    account_number: Optional[str]
    provider: Optional[str]
    is_active: bool

    model_config = {"from_attributes": True}
