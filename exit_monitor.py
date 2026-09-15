import json
import os
import time
from decimal import Decimal
from pathlib import Path

from eth_account import Account

from live_executor import (
    HUB,
    PRIVATE_KEY,
    USDC,
    call_rpc,
    make_tx,
    read_uint,
    sign_and_send,
    word_address,
    word_uint,
)

STATE = "029282d7"
PAIR = "e6a43905"
BALANCE = "70a08231"
BUY_EVENT = "7c97dafb65ab77a2f0d9365ac3d1c9bb558423c1a2a60f6e1a74278526d61b4e"
UNBOND = "ede94c22"
CLAIM = "1e83409a"
SELL = "6a272462"
RESERVES = "0902f1ac"
TOKEN0 = "0dfe1681"
TOKEN1 = "d21220a7"
PATH = Path(os.getenv("AUTO_STATE_PATH", "auto_strategy_state.json"))
AUTO_EXIT_LIVE = os.getenv("AUTO_EXIT_LIVE", "0") == "1"
AUTO_EXIT_SLIPPAGE_BPS = max(0, min(5000, int(os.getenv("AUTO_EXIT_SLIPPAGE_BPS", "1000"))))
SETTLE_FEE_USDC = int(Decimal(os.getenv("AUTO_EXIT_SETTLE_FEE_USDC", "0.02")) * 10**6)
UNBOND_WAIT_SECONDS = 90
TRANSFER = "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def rpc_call(data, to=HUB, from_address=None, block_tag="latest"):
    tx = {"to": to, "data": data}
    if from_address:
        tx["from"] = from_address
    return call_rpc("eth_call", [tx, block_tag])


def words(value):
    raw = (value or "0x")[2:]
    if not raw or len(raw) % 64:
        return []
    return [int(raw[index : index + 64], 16) for index in range(0, len(raw), 64)]


def owner_address():
    return Account.from_key(PRIVATE_KEY).address if PRIVATE_KEY else ""


def read_state():
    try:
        value = json.loads(PATH.read_text())
        if isinstance(value, dict):
            value.setdefault("bought", [])
            return value
    except Exception:
        pass
    return {"bought": []}


def save_state(value):
    PATH.write_text(json.dumps(value, indent=2, sort_keys=True))


def receipt_position(tx_hash, token, owner):
    if not tx_hash:
        return 0
    try:
        receipt = call_rpc("eth_getTransactionReceipt", [tx_hash])
        for log in (receipt or {}).get("logs", []):
            topics = log.get("topics") or []
            if len(topics) < 3 or topics[0].lower() != "0x" + BUY_EVENT:
                continue
            if not topics[1].lower().endswith(token[2:].lower()):
                continue
            if not topics[2].lower().endswith(owner[2:].lower()):
                continue
            data = words(log.get("data"))
            if len(data) >= 3:
                return data[2]
    except Exception:
        return 0
    return 0


def launch_info(token):
    return words(rpc_call("0x" + STATE + word_address(token)))


def pair_for(token):
    value = words(rpc_call("0x" + PAIR + word_address(token) + word_address(USDC)))
    return "0x" + f"{value[0]:040x}" if value else ""


def is_graduated(token):
    pair = pair_for(token)
    launch = launch_info(token)
    return bool(pair and len(launch) >= 6 and launch[1] and launch[5] and int(pair, 16) != 0), pair


def unbond_data(token, amount):
    # Observed successful Flipt calldata: unbond(token, amount, 1, 0, 0).
    return "0x" + UNBOND + word_address(token) + word_uint(amount) + word_uint(1) + word_uint(0) + word_uint(0)


def claim_data(token):
    return "0x" + CLAIM + word_address(token)


def sell_data(token, amount, min_out):
    return "0x" + SELL + word_address(token) + word_uint(amount) + word_uint(min_out)


def receipt_status(tx_hash):
    if not tx_hash:
        return None
    receipt = call_rpc("eth_getTransactionReceipt", [tx_hash])
    if not receipt:
        return None
    return receipt.get("status")


def token_balance(token, owner):
    return read_uint(token, "0x" + BALANCE + word_address(owner))


def fee_bps_for_mcap(mcap_usd):
    if mcap_usd < Decimal("110250"):
        return 175
    if mcap_usd < Decimal("184500"):
        return 170
    if mcap_usd < Decimal("258000"):
        return 165
    if mcap_usd < Decimal("331500"):
        return 160
    if mcap_usd < Decimal("736500"):
        return 155
    if mcap_usd < Decimal("1105500"):
        return 150
    if mcap_usd < Decimal("1473750"):
        return 145
    if mcap_usd < Decimal("1842000"):
        return 140
    if mcap_usd < Decimal("2210250"):
        return 135
    if mcap_usd < Decimal("2578500"):
        return 130
    if mcap_usd < Decimal("2947500"):
        return 120
    if mcap_usd < Decimal("3315750"):
        return 115
    if mcap_usd < Decimal("3684000"):
        return 110
    if mcap_usd < Decimal("4052250"):
        return 107
    if mcap_usd < Decimal("4420500"):
        return 105
    if mcap_usd < Decimal("4789500"):
        return 102
    return 100


def sell_quote(token, pair, amount, owner):
    token0 = "0x" + rpc_call("0x" + TOKEN0, to=pair)[-40:]
    reserves = words(rpc_call("0x" + RESERVES, to=pair))
    if len(reserves) < 2 or amount <= 0:
        return {"status": "quote_unavailable"}
    if token0.lower() == USDC.lower():
        usdc_reserve, token_reserve = reserves[0], reserves[1]
    else:
        token_reserve, usdc_reserve = reserves[0], reserves[1]
    if token_reserve <= 0 or usdc_reserve <= 0:
        return {"status": "quote_unavailable"}

    gross = (usdc_reserve * amount) // (token_reserve + amount)
    spot_mcap = (Decimal(usdc_reserve) / Decimal(10**6)) / (Decimal(token_reserve) / Decimal(10**18)) * Decimal(10**9)
    fee_bps = fee_bps_for_mcap(spot_mcap)
    net_before_gas = gross * (10000 - fee_bps) // 10000
    min_out = net_before_gas * (10000 - AUTO_EXIT_SLIPPAGE_BPS) // 10000
    gas_cost = None
    try:
        gas = int(call_rpc("eth_estimateGas", [{"from": owner, "to": HUB, "data": sell_data(token, amount, min_out), "value": "0x0"}]), 16)
        gas_price = int(call_rpc("eth_gasPrice"), 16)
        gas_cost = gas * gas_price
    except Exception:
        pass
    return {
        "status": "quoted",
        "spot_mcap_usd": str(spot_mcap.quantize(Decimal("0.01"))),
        "gross_usdc": str(Decimal(gross) / Decimal(10**6)),
        "fee_bps": fee_bps,
        "net_before_gas_usdc": str(Decimal(net_before_gas) / Decimal(10**6)),
        "min_tokens_sell_out": str(min_out),
        "gas_cost_usdc": str(Decimal(gas_cost) / Decimal(10**6)) if gas_cost is not None else None,
        "settle_fee_usdc": str(Decimal(SETTLE_FEE_USDC) / Decimal(10**6)),
        "quote_method": "constant_product_reserves_then_official_graduated_fee",
    }


def check():
    saved = read_state()
    owner = owner_address()
    if not owner:
        return {"status": "waiting_signer", "auto_exit_live": AUTO_EXIT_LIVE}
    rows = []
    for item in saved.get("bought", []):
        token = item.get("token", "")
        if not token:
            continue
        row = {"token": token, "cost_usdc": item.get("amount_usdc"), "buy_tx": item.get("tx_hash", ""), "unbond_selector": "0x" + UNBOND, "claim_selector": "0x" + CLAIM, "sell_selector": "0x" + SELL}
        try:
            graduated, pair = is_graduated(token)
            wallet_balance = token_balance(token, owner)
            position = receipt_position(item.get("tx_hash", ""), token, owner)
            row.update({"graduated": graduated, "pair": pair, "wallet_token_balance": str(wallet_balance), "bonded_token_amount": str(position)})
            if not graduated:
                row["status"] = "waiting_graduation"
            elif wallet_balance > 0:
                row["status"] = "wallet_ready_for_sell_quote"
                row["sell_quote"] = sell_quote(token, pair, wallet_balance, owner)
            elif position > 0:
                row["status"] = "graduated_bonded_exit_needs_unbond_and_claim"
                try:
                    row["unbond_simulation"] = {"status": "ok", "return_data": rpc_call(unbond_data(token, position), from_address=owner)}
                except Exception as exc:
                    row["unbond_simulation"] = {"status": "reverted", "error": str(exc)}
                if AUTO_EXIT_LIVE:
                    row["status"] = "blocked_live_exit_requires_explicit_runtime_review"
            else:
                row["status"] = "waiting_unbond_or_claim"
        except Exception as exc:
            row["status"] = "read_error"
            row["error"] = str(exc)
        rows.append(row)
    return {"status": "ok", "owner": owner, "positions": rows, "auto_exit_live": AUTO_EXIT_LIVE, "sell_loop": "fail_closed_until_unbond_claim_quote_and_net_profit_are_verified", "unbond_wait_seconds": UNBOND_WAIT_SECONDS, "settle_fee_usdc": str(Decimal(SETTLE_FEE_USDC) / Decimal(10**6))}
