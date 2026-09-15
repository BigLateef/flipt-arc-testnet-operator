import json
import os
import time
from decimal import Decimal
from pathlib import Path

from eth_account import Account

from live_executor import (
    HUB,
    USDC,
    DRY_RUN,
    LIVE_TRADING,
    PRIVATE_KEY,
    MAX_USDC_PER_LAUNCH,
    call_rpc,
    make_tx,
    read_uint,
    sign_and_send,
    word_address,
    word_uint,
)

COUNT = "27cca59f"
LIST = "7b443a76"
STATE = "029282d7"
MIN_FDV = Decimal(os.getenv("AUTO_MIN_FDV_USD", "2000"))
AMOUNT = min(
    Decimal(os.getenv("AUTO_BUY_USDC_AMOUNT", os.getenv("BUY_USDC_AMOUNT", "25"))),
    MAX_USDC_PER_LAUNCH,
)
MAX_BUYS = int(os.getenv("MAX_AUTO_BUYS", "3"))
PATH = Path(os.getenv("AUTO_STATE_PATH", "auto_strategy_state.json"))
# Blank RUN_END_AT means no automatic expiry. Set an ISO timestamp to enable one.
END = os.getenv("RUN_END_AT", "").strip()


def call(selector, arg=None):
    data = "0x" + selector + (word_uint(arg) if arg is not None else "")
    return call_rpc("eth_call", [{"to": HUB, "data": data}, "latest"])


def words(value):
    raw = (value or "0x")[2:]
    if not raw or len(raw) % 64:
        return []
    return [int(raw[i : i + 64], 16) for i in range(0, len(raw), 64)]


def state():
    try:
        value = json.loads(PATH.read_text())
        if isinstance(value, dict):
            return value
    except Exception:
        pass
    return {"seen": [], "bought": []}


def save(value):
    PATH.write_text(json.dumps(value, indent=2, sort_keys=True))


def discover(limit=10):
    saved = state()
    seen = {str(value).lower() for value in saved.get("seen", [])}
    out = []
    count_words = words(call(COUNT))
    count = count_words[0] if count_words else 0

    for index in range(max(0, count - limit), count):
        launch_words = words(call(LIST, index))
        if not launch_words:
            continue
        token = "0x" + f"{launch_words[0]:040x}"
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        saved["seen"] = list(seen)[-500:]

        values = words(call(STATE, int(token[2:], 16)))
        if len(values) < 9 or not values[0] or (values[1] and values[5]):
            continue

        raised = Decimal(values[2]) / Decimal(10**6)
        sold = Decimal(values[3]) / Decimal(10**18)
        fdv = (raised * Decimal(10**9) / sold) if raised and sold else Decimal("2100")
        out.append(
            {
                "token": token,
                "fdv_usd": str(fdv.quantize(Decimal("0.01"))),
                "raised_usdc": str(raised),
                "sold_tokens": str(sold),
                "index": index,
            }
        )

    save(saved)
    return out, saved


def min_out(item):
    raised = Decimal(item["raised_usdc"])
    sold = Decimal(item["sold_tokens"])
    expected = (
        AMOUNT * sold / raised
        if raised and sold
        else (AMOUNT / Decimal("2100")) * Decimal(10**9)
    )
    return max(1, int(expected * Decimal("0.50")))


def buy(item):
    if DRY_RUN or not LIVE_TRADING:
        return {"ok": False, "status": "blocked", "reason": "live switches are not enabled"}
    if not PRIVATE_KEY:
        return {"ok": False, "status": "blocked", "reason": "burner is not configured"}
    if AMOUNT <= 0:
        return {"ok": False, "status": "blocked", "reason": "auto amount is zero"}

    owner = Account.from_key(PRIVATE_KEY).address
    amount = int(AMOUNT * Decimal(10**6))
    balance = read_uint(USDC, "0x70a08231" + word_address(owner))
    if balance < amount:
        return {
            "ok": False,
            "status": "blocked",
            "reason": "insufficient Flipt ERC-20 USDC",
            "wallet": owner,
            "usdc_balance": balance,
        }

    allowance = read_uint(
        USDC,
        "0xdd62ed3e" + word_address(owner) + word_address(HUB),
    )
    nonce = int(call_rpc("eth_getTransactionCount", [owner, "pending"]), 16)

    if allowance != amount:
        tx = make_tx(
            owner,
            USDC,
            "0x095ea7b3" + word_address(HUB) + word_uint(amount),
            nonce,
        )
        return {
            "ok": True,
            "status": "approval_broadcast",
            "tx_hash": sign_and_send(tx),
            "wallet": owner,
            "token": item["token"],
            "amount_usdc": str(AMOUNT),
        }

    data = (
        "0xa59ac6dd"
        + word_address(item["token"])
        + word_uint(amount)
        + word_uint(min_out(item))
    )
    tx = make_tx(owner, HUB, data, nonce)
    return {
        "ok": True,
        "status": "buy_broadcast",
        "tx_hash": sign_and_send(tx),
        "wallet": owner,
        "token": item["token"],
        "amount_usdc": str(AMOUNT),
        "min_tokens_out": min_out(item),
    }


def run_once():
    try:
        if END and time.time() >= time.mktime(
            time.strptime(END[:19], "%Y-%m-%dT%H:%M:%S")
        ):
            return {"ok": True, "status": "ended"}

        items, saved = discover()
        skip = os.getenv("TARGET_LAUNCH_ADDRESS", "").lower()
        bought = {
            value.get("token", "").lower() for value in saved.get("bought", [])
        }
        candidates = [
            value
            for value in items
            if value["token"].lower() != skip
            and value["token"].lower() not in bought
            and Decimal(value["fdv_usd"]) >= MIN_FDV
        ]

        if len(bought) >= MAX_BUYS:
            return {"ok": True, "status": "cap_reached", "bought_count": len(bought)}
        if not candidates:
            return {
                "ok": True,
                "status": "no_candidate",
                "discovered": len(items),
                "bought_count": len(bought),
            }

        item = candidates[0]
        result = buy(item)
        if result.get("status") == "buy_broadcast":
            saved.setdefault("bought", []).append(
                {**item, **result, "time": int(time.time())}
            )
            save(saved)

        return {
            "ok": result.get("ok", False),
            "status": result.get("status"),
            "launch": item,
            "result": result,
        }
    except Exception as exc:
        return {"ok": False, "status": "preflight_blocked", "error": str(exc)}
