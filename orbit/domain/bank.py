"""Bank statement imports and the transactions they brought in."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from orbit.domain.base import Base, TimestampMixin, enum_column, utcnow
from orbit.domain.enums import Bank, Classification, TransactionDirection, TransactionKind


class BankImport(Base):
    """One uploaded statement file and what came of it."""

    __tablename__ = "bank_import"

    id: Mapped[int] = mapped_column(primary_key=True)
    bank: Mapped[Bank] = mapped_column(enum_column(Bank, "bank"))
    filename: Mapped[str | None] = mapped_column(String(255))
    imported_at: Mapped[datetime] = mapped_column(default=utcnow)
    rows_total: Mapped[int] = mapped_column(default=0)
    rows_new: Mapped[int] = mapped_column(default=0)
    rows_duplicate: Mapped[int] = mapped_column(default=0)
    # Rows the parser leaves out on purpose (e.g. Revolut rows that aren't COMPLETED).
    rows_skipped: Mapped[int] = mapped_column(default=0)

    transactions: Mapped[list[Transaction]] = relationship(back_populates="bank_import")


class Transaction(TimestampMixin, Base):
    """A bank row, normalized. Amounts are positive; `direction` says which way the money went."""

    __tablename__ = "bank_transaction"
    __table_args__ = (CheckConstraint("amount_minor > 0", name="amount_positive"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    import_id: Mapped[int] = mapped_column(
        ForeignKey("bank_import.id", ondelete="CASCADE"), index=True
    )
    bank: Mapped[Bank] = mapped_column(enum_column(Bank, "bank"))
    booking_date: Mapped[date] = mapped_column(index=True)
    # When the payment happened (card time / start time), if the statement says; UTC.
    occurred_at: Mapped[datetime | None]
    amount_minor: Mapped[int]
    currency: Mapped[str] = mapped_column(String(3))
    direction: Mapped[TransactionDirection] = mapped_column(
        enum_column(TransactionDirection, "transaction_direction")
    )
    kind: Mapped[TransactionKind] = mapped_column(enum_column(TransactionKind, "transaction_kind"))
    # Merchant or the other party's name as the bank shows it.
    counterparty: Mapped[str | None] = mapped_column(Text)
    counterparty_norm: Mapped[str | None] = mapped_column(Text)
    # The other party's IBAN (compact) for matching IBAN handles; shown and sent only masked.
    counterparty_iban: Mapped[str | None] = mapped_column(String(34), index=True)
    iban_masked: Mapped[str | None] = mapped_column(String(20))
    message: Mapped[str | None] = mapped_column(Text)
    message_norm: Mapped[str | None] = mapped_column(Text)
    bank_reference: Mapped[str | None] = mapped_column(String(100))
    classification: Mapped[Classification | None] = mapped_column(
        enum_column(Classification, "classification")
    )
    hash: Mapped[str] = mapped_column(String(64), unique=True)
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSON)

    bank_import: Mapped[BankImport] = relationship(back_populates="transactions")

    @property
    def label(self) -> str:
        """What to call it in the UI: the merchant, else the masked IBAN, else the kind."""
        return self.counterparty or self.iban_masked or self.kind.value.capitalize()
