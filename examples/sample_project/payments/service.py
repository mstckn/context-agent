"""Payment operations: invoices and refunds.

Uses the database transaction contract from ``db.connection`` and the
analytics tracker for audit events.
"""

from payments.models import Invoice, Refund
from db.connection import transaction
from analytics.events import track_event

_invoices = {}
_refunds = {}


def create_invoice(customer_id, amount_cents, currency="USD"):
    """Create and persist a new invoice for a customer."""
    invoice_id = f"inv_{len(_invoices) + 1:05d}"
    invoice = Invoice(invoice_id=invoice_id, customer_id=customer_id,
                      amount_cents=amount_cents, currency=currency)
    with transaction() as conn:
        conn.execute("INSERT INTO invoices VALUES (?,?,?,?)",
                     (invoice_id, customer_id, amount_cents, currency))
    _invoices[invoice_id] = invoice
    track_event("invoice.created", {"invoice_id": invoice_id,
                                    "amount_cents": amount_cents})
    return invoice


def process_refund(invoice_id, amount_cents=None, reason=""):
    """Refund part or all of an invoice.

    Raises ValueError if the invoice is missing or the amount exceeds
    the invoice total.
    """
    invoice = _invoices.get(invoice_id)
    if invoice is None:
        raise ValueError(f"unknown invoice {invoice_id}")
    amount_cents = amount_cents or invoice.amount_cents
    if amount_cents > invoice.amount_cents:
        raise ValueError("refund exceeds invoice amount")

    refund_id = f"ref_{len(_refunds) + 1:05d}"
    refund = Refund(refund_id=refund_id, invoice_id=invoice_id,
                    amount_cents=amount_cents, reason=reason,
                    status="completed")
    with transaction() as conn:
        conn.execute("INSERT INTO refunds VALUES (?,?,?)",
                     (refund_id, invoice_id, amount_cents))
        conn.execute("UPDATE invoices SET status='refunded' WHERE id=?",
                     (invoice_id,))
    _refunds[refund_id] = refund
    invoice.status = "refunded"
    track_event("refund.processed", {"refund_id": refund_id,
                                     "invoice_id": invoice_id})
    return refund


def charge_customer(customer_id, amount_cents):
    """Charge a customer by creating a paid invoice."""
    invoice = create_invoice(customer_id, amount_cents)
    invoice.status = "paid"
    return invoice
