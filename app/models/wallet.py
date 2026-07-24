"""Wallet models — balance, transactions, virtual accounts, withdrawals."""

import enum
from sqlalchemy import Column, String, Text, Numeric, ForeignKey, Boolean
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from .base import BaseModel


class TransactionType(str, enum.Enum):
    CREDIT = "credit"           # Money added to wallet
    DEBIT = "debit"             # Money spent from wallet
    WITHDRAWAL = "withdrawal"   # Payout to bank
    ESCROW_HOLD = "escrow_hold"     # Held for a contract
    ESCROW_RELEASE = "escrow_release"  # Released from escrow
    REFUND = "refund"           # Returned to wallet


class TransactionStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    SUCCESS = "success"
    FAILED = "failed"
    REVERSED = "reversed"


class WithdrawalStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    SUCCESS = "success"
    FAILED = "failed"
    REJECTED = "rejected"


class Wallet(BaseModel):
    __tablename__ = "wallets"

    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False, index=True)
    balance = Column(Numeric(12, 2), default=0, nullable=False)
    total_earned = Column(Numeric(12, 2), default=0, nullable=False)
    total_withdrawn = Column(Numeric(12, 2), default=0, nullable=False)
    total_spent = Column(Numeric(12, 2), default=0, nullable=False)
    is_frozen = Column(Boolean, default=False)  # Admin can freeze wallet

    # Relationships
    user = relationship("User", backref="wallet")
    transactions = relationship("WalletTransaction", back_populates="wallet", cascade="all, delete-orphan", order_by="WalletTransaction.created_at.desc()")


class WalletTransaction(BaseModel):
    __tablename__ = "wallet_transactions"

    wallet_id = Column(UUID(as_uuid=True), ForeignKey("wallets.id", ondelete="CASCADE"), nullable=False, index=True)
    type = Column(String(20), nullable=False, index=True)             # TransactionType
    amount = Column(Numeric(12, 2), nullable=False)
    fee = Column(Numeric(12, 2), default=0)                           # Platform / gateway fee
    balance_before = Column(Numeric(12, 2), nullable=True)
    balance_after = Column(Numeric(12, 2), nullable=True)
    description = Column(Text, nullable=True)
    reference = Column(String(100), unique=True, index=True, nullable=True)   # Payment gateway ref
    provider = Column(String(30), nullable=True)                      # paystack | flutterwave | manual
    status = Column(String(20), default="pending", nullable=False, index=True)
    extra_data = Column(Text, nullable=True)  # JSON string for extra data

    # Relationships
    wallet = relationship("Wallet", back_populates="transactions")


class VirtualAccount(BaseModel):
    __tablename__ = "virtual_accounts"

    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False, index=True)
    bank_name = Column(String(100), nullable=True)
    account_name = Column(String(200), nullable=True)
    account_number = Column(String(20), nullable=True)
    provider = Column(String(50), nullable=True)        # paystack | flutterwave
    customer_code = Column(String(100), nullable=True)  # Provider's customer identifier
    dva_id = Column(String(100), nullable=True)         # Dedicated virtual account ID at provider
    is_active = Column(Boolean, default=True)

    # Relationships
    user = relationship("User", backref="virtual_account")


class WithdrawalRequest(BaseModel):
    __tablename__ = "withdrawal_requests"

    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    wallet_id = Column(UUID(as_uuid=True), ForeignKey("wallets.id", ondelete="CASCADE"), nullable=False)
    amount = Column(Numeric(12, 2), nullable=False)
    fee = Column(Numeric(12, 2), default=0)
    net_amount = Column(Numeric(12, 2), nullable=False)   # amount - fee
    bank_name = Column(String(100), nullable=False)
    account_name = Column(String(200), nullable=False)
    account_number = Column(String(20), nullable=False)
    bank_code = Column(String(20), nullable=True)         # For automated transfer
    status = Column(String(20), default="pending", nullable=False, index=True)
    reference = Column(String(100), unique=True, index=True, nullable=True)
    provider = Column(String(30), nullable=True)          # paystack | flutterwave | manual
    rejection_reason = Column(Text, nullable=True)
    processed_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    # Relationships
    user = relationship("User", foreign_keys=[user_id], backref="withdrawal_requests")
    wallet = relationship("Wallet")
