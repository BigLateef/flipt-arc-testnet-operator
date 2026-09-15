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
# Keep the existing one-buy-per-tick risk profile unless the operator raises it.
BUYS_PER_TICK = max(1, int(os.getenv("AUTO_BUYS_PER_TICK", "1")))
SCAN_BATCH = max(1, int(os.getenv("AUTO_SCAN_BATCH", "120")))
STARTUP_BACKFILL = max(1, int(os.getenv("AUTO_STARTUP_BACKFILL", "500")))
MAX_PENDING = max(100, int(os.getenv("AUTO_MAX_PENDING", "2000")))
PATH = Path(os.getenv("AUTO_STATE_PATH", "auto_strategy_state.json"))
# Blank RUN_END_AT means no automatic expiry. Set an ISO timestamp to enable one.
END = os.getenv("RUN_END_AT", "").strip()


def call(selector, arg=None, block_tag="latest"):
    data = "0x" + selector + (word_uint(arg) if arg is not None else "")
    return call_rpc("eth_call", [{"to": HUB, "data": data}, block_tag])


def words(value):
    raw = (value or "0x")[2:]
    if not raw or len(raw) % 64:
        return []
    return [int(raw[index : index + 64], 16) for index in range(0, len(raw), 64)]


def state():
    try:
        value = json.loads(PATH.read_text())
        if isinstance(value, dict):
            value.setdefault("bought", [])
            value.setdefault("pending", [])
            value.setdefault("inflight", [])
            return value
    except Exception:
        pass
    return {"version": 2, "next_index": None, "pending": [], "bought": [], "inflight": []}


def save(value):
    value["version"] = 2
    PATH.write_text(json.dumps(value, indent=2, sort_keys=True))


def launch_item(token, index=None):
    values = words(call(STATE, int(token[2:], 16)))
    if len(values) < 9 or not values[0]:
        return None
    # v1 + v5 indicate a graduated/closed launch in the verified read tuple.
    if values[1] and values[5]:
        return None
    raised = Decimal(values[2]) / Decimal(10**6)
    sold = Decimal(values[3]) / Decimal(10**18)
    fdv = (raised * Decimal(10**9) / sold) if raised and sold else Decimal("2100")
    return {
        "token": token,
        "fdv_usd": str(fdv.quantize(Decimal("0.01"))),
        "raised_usdc": str(raised),
        "sold_tokens": str(sold),
        "index": index,
    }


def token_from_index(index):
    launch_words = words(call(LIST, index))
    if not launch_words:
        return ""
    return "0x" + f"{launch_words[0]:040x}"


def merge_pending(saved, item):
    token = item["token"].lower()
    for index, existing in enumerate(saved["pending"]):
        if existing.get("token", "").lower() == token:
            # Preserve approval/retry bookkeeping while refreshing quote data.
            for key, value in item.items():
                existing[key] = value
            saved["pending"][index] = existing
            return
    saved["pending"].append(item)


def remove_pending(saved, token):
    token = token.lower()
    saved["pending"] = [
        item for item in saved.get("pending", [])
        if item.get("token", "").lower() != token
    ]


def reconcile_inflight(saved):
    remaining = []
    for item in saved.get("inflight", []):
        tx_hash = item.get("tx_hash", "")
        if not tx_hash:
            continue
        try:
            receipt = call_rpc("eth_getTransactionReceipt", [tx_hash])
        except Exception:
            receipt = None
        if not receipt:
            remaining.append(item)
            continue
        if receipt.get("status") == "0x1":
            saved.setdefault("bought", []).append(item)
        else:
            # A reverted buy is retryable only while the launch remains active.
            item.pop("tx_hash", None)
            item["last_error"] = "buy transaction reverted"
            item["retry_after"] = int(time.time()) + 30
            merge_pending(saved, item)
    saved["inflight"] = remaining


def discover(saved, count):
    """Advance a durable scan cursor; do not permanently discard unseen launches."""
    if saved.get("next_index") is None:
        # Migrate the old {seen,bought} state after a deploy/restart.
        saved["next_index"] = max(0, count - STARTUP_BACKFILL)
    start = max(0, int(saved.get("next_index", 0)))
    end = min(count, start + SCAN_BATCH)
    bought = {item.get("token", "").lower() for item in saved.get("bought", [])}
    inflight = {item.get("token", "").lower() for item in saved.get("inflight", [])}
    for index in range(start, end):
        token = token_from_index(index)
        if not token or token.lower() in bought or token.lower() in inflight:
            continue
        item = launch_item(token, index)
        if item is not None:
            merge_pending(saved, item)
    saved["next_index"] = end
    # Refresh pending launches so FDV crossings are noticed and graduated launches
    # are removed before signing. The queue is bounded and oldest-first.
    refreshed = []
    for item in saved.get("pending", [])[:MAX_PENDING]:
        token = item.get("token", "")
        if not token or token.lower() in bought or token.lower() in inflight:
            continue
        try:
            current = launch_item(token, item.get("index"))
        except Exception:
            current = item
        if current is not None:
            for key in ("approval_tx", "last_error", "retry_after"):
                if key in item:
                    current[key] = item[key]
            refreshed.append(current)
    saved["pending"] = refreshed
    return {"scanned_from": start, "scanned_to": end, "pending": len(refreshed)}


def min_out(item):
    raised = Decimal(item["raised_usdc"])
    sold = Decimal(item["sold_tokens"])
    expected = (
        AMOUNT * sold / raised
        if raised and sold
        else (AMOUNT / Decimal("2100")) * Decimal(10**9)
    )
    return max(1, int(expected * Decimal("0.50")))


def receipt_status(tx_hash):
    if not tx_hash:
        return None
    receipt = call_rpc("eth_getTransactionReceipt", [tx_hash])
    if not receipt:
        return None
    return receipt.get("status")


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
        return {"ok": False, "status": "blocked", "reason": "insufficient Flipt ERC-20 USDC", "wallet": owner, "usdc_balance": balance}
    allowance = read_uint(USDC, "0xdd62ed3e" + word_address(owner) + word_address(HUB))
    nonce = int(call_rpc("eth_getTransactionCount", [owner, "pending"]), 16)
    # Approval is durable; keep the candidate and never spam while it is pending.
    if allowance != amount:
        approval_tx = item.get("approval_tx", "")
        if approval_tx:
            status = receipt_status(approval_tx)
            if status is None:
                return {"ok": True, "status": "approval_pending", "tx_hash": approval_tx}
            if status == "0x1":
                return {"ok": True, "status": "approval_confirmed", "tx_hash": approval_tx}
            item.pop("approval_tx", None)
        tx = make_tx(owner, USDC, "0x095ea7b3" + word_address(HUB) + word_uint(amount), nonce)
        return {"ok": True, "status": "approval_broadcast", "tx_hash": sign_and_send(tx), "wallet": owner, "token": item["token"], "amount_usdc": str(AMOUNT)}
    # Recompute min_out from current state immediately before signing.
    data = "0xa59ac6dd" + word_address(item["token"]) + word_uint(amount) + word_uint(min_out(item))
    tx = make_tx(owner, HUB, data, nonce)
    return {"ok": True, "status": "buy_broadcast", "tx_hash": sign_and_send(tx), "wallet": owner, "token": item["token"], "amount_usdc": str(AMOUNT), "min_tokens_out": min_out(item)}


def run_once():
    try:
        if END and time.time() >= time.mktime(time.strptime(END[:19], "%Y-%m-%dT%H:%M:%S")):
            return {"ok": True, "status": "ended"}
        saved = state()
        reconcile_inflight(saved)
        count_words = words(call(COUNT))
        count = count_words[0] if count_words else 0
        scan = discover(saved, count)
        bought = {item.get("token", "").lower() for item in saved.get("bought", [])}
        skip = os.getenv("TARGET_LAUNCH_ADDRESS", "").lower()
        now = int(time.time())
        candidates = [
            item for item in saved.get("pending", [])
            if item.get("token", "").lower() != skip
            and item.get("token", "").lower() not in bought
            and Decimal(item.get("fdv_usd", "0")) >= MIN_FDV
            and int(item.get("retry_after", 0)) <= now
        ]
        candidates.sort(key=lambda item: int(item.get("index", 0)))
        if len(bought) >= MAX_BUYS:
            save(saved)
            return {"ok": True, "status": "cap_reached", "bought_count": len(bought), "scan": scan}
        if not candidates:
            save(saved)
            return {"ok": True, "status": "no_candidate", "discovered": scan["pending"], "bought_count": len(bought), "scan": scan}
        actions = []
        for item in candidates[:BUYS_PER_TICK]:
            # Final state revalidation closes the graduated/stale-candidate race.
            current = launch_item(item["token"], item.get("index"))
            if current is None:
                remove_pending(saved, item["token"])
                continue
            for key in ("approval_tx", "last_error", "retry_after"):
                if key in item:
                    current[key] = item[key]
            result = buy(current)
            status = result.get("status")
            if status == "approval_broadcast":
                current["approval_tx"] = result.get("tx_hash", "")
                merge_pending(saved, current)
            elif status in ("approval_confirmed", "approval_pending"):
                merge_pending(saved, current)
            elif status == "buy_broadcast":
                remove_pending(saved, current["token"])
                saved.setdefault("inflight", []).append({**current, **result, "time": now})
            elif not result.get("ok"):
                current["last_error"] = result.get("reason", "buy blocked")
                current["retry_after"] = now + 60
                merge_pending(saved, current)
            actions.append({"launch": current, "result": result})
            # Never broadcast multiple approvals in one tick with the same nonce.
            if status in ("approval_broadcast", "approval_pending", "approval_confirmed"):
                break
        save(saved)
        first = actions[0] if actions else {"result": {"status": "no_action"}}
        return {"ok": first["result"].get("ok", False), "status": first["result"].get("status"), "actions": actions, "scan": scan, "bought_count": len(bought)}
    except Exception as exc:
        return {"ok": False, "status": "preflight_blocked", "error": str(exc)}
