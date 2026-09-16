"""Invoice and refund models."""

from dataclasses import dataclass


@dataclass
class Invoice:
    """A billable invoice for a customer."""

    invoice_id: str
    customer_id: str
    amount_cents: int
    currency: str = "USD"
    status: str = "open"


@dataclass
class Refund:
    """A refund issued against an invoice."""

    refund_id: str
    invoice_id: str
    amount_cents: int
    reason: str = ""
    status: str = "pending"
