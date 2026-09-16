"""Tests for the payments module."""

from payments.service import create_invoice, process_refund, charge_customer


def test_create_invoice():
    invoice = create_invoice("cust_1", 5000)
    assert invoice.status == "open"
    assert invoice.amount_cents == 5000


def test_process_full_refund():
    invoice = create_invoice("cust_2", 3000)
    refund = process_refund(invoice.invoice_id)
    assert refund.amount_cents == 3000
    assert refund.status == "completed"


def test_refund_overpay_rejected():
    invoice = create_invoice("cust_3", 1000)
    try:
        process_refund(invoice.invoice_id, amount_cents=2000)
        assert False
    except ValueError:
        pass


def test_charge_customer_marks_paid():
    invoice = charge_customer("cust_4", 750)
    assert invoice.status == "paid"
