"""Command-line interface commands.

Each command is a function taking the parsed argument list. Register new
commands in the ``COMMANDS`` table at the bottom of this file.
"""

import sys

from payments.service import process_refund
from utils.logging import get_logger

logger = get_logger("cli")


def cmd_status(args):
    """Show a short status line for the application."""
    print("sample-project status: ok")
    return 0


def cmd_refund(args):
    """Refund an invoice: ``refund <invoice_id> [amount_cents]``."""
    if not args:
        print("usage: refund <invoice_id> [amount_cents]", file=sys.stderr)
        return 2
    invoice_id = args[0]
    amount = int(args[1]) if len(args) > 1 else None
    refund = process_refund(invoice_id, amount_cents=amount, reason="cli")
    print(f"refunded {refund.amount_cents} via {refund.refund_id}")
    return 0


COMMANDS = {
    "status": cmd_status,
    "refund": cmd_refund,
}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in COMMANDS:
        print(f"available commands: {', '.join(sorted(COMMANDS))}",
              file=sys.stderr)
        return 2
    return COMMANDS[argv[0]](argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
