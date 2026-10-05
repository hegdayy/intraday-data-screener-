"""
broker.py — paper-trading broker.

Every order is simulated: it is logged in memory and reported back as a
successful fill at the requested price. No real orders are ever sent.
"""
import datetime
from typing import List, Optional

PAPER_CASH = 100_000.0     # simulated account balance (Rs)
_orders: List[dict] = []


def set_safety(on: bool):
    """Kept for API compatibility — paper mode is always on."""
    return None


def is_safety_on() -> bool:
    return True


def _record(txn: str, sym: str, qty: float, price: Optional[float], note: str = "") -> dict:
    _orders.append({
        "order_id": f"P{len(_orders) + 1:04d}",
        "order_timestamp": datetime.datetime.now().strftime("%H:%M:%S"),
        "tradingsymbol": sym, "transaction_type": txn,
        "quantity": int(qty), "filled_quantity": int(qty),
        "average_price": float(price or 0), "status": "COMPLETE",
        "status_message": note,
    })
    return {"status": "SAFETY_DRY_RUN", "order_id": None, "symbol": sym,
            "qty": qty, "action": txn}


def broker_buy(sym: str, qty: float, price: Optional[float] = None,
               order_type: str = "MARKET", direction: str = "LONG",
               product: str = "MIS", tag: Optional[str] = None,
               parent=None) -> dict:
    txn = "SELL" if direction == "SHORT" else "BUY"
    return _record(txn, sym, qty, price)


def broker_sell(sym: str, qty: float, price: Optional[float] = None,
                order_type: str = "MARKET", direction: str = "LONG",
                product: str = "MIS", reason: Optional[str] = None,
                tag: Optional[str] = None, parent=None) -> dict:
    txn = "BUY" if direction == "SHORT" else "SELL"
    return _record(txn, sym, qty, price, reason or "")


def get_available_funds() -> float:
    return PAPER_CASH


def get_order_book() -> List[dict]:
    return list(_orders)


def get_market_depth(symbol: str) -> dict:
    raise RuntimeError("market depth is not available in paper mode")
