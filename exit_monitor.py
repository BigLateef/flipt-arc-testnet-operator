import json
import os
from pathlib import Path

from eth_account import Account

from live_executor import HUB, PRIVATE_KEY, USDC, call_rpc, word_address

STATE = "029282d7"
PAIR = "e6a43905"
BALANCE = "70a08231"
BUY_EVENT = "7c97dafb65ab77a2f0d9365ac3d1c9bb558423c1a2a60f6e1a74278526d61b4e"
CLAIM = "1e83409a"
SELL = "6a272462"
PATH = Path(os.getenv("AUTO_STATE_PATH", "auto_strategy_state.json"))
AUTO_EXIT_LIVE = os.getenv("AUTO_EXIT_LIVE", "0") == "1"


def rpc_call(data, to=HUB):
    return call_rpc("eth_call", [{"to": to, "data": data}, "latest"])


def words(value):
    raw = (value or "0x")[2:]
    if not raw or len(raw) % 64:
        return []
    return [int(raw[i : i + 64], 16) for i in range(0, len(raw), 64)]


def owner_address():
    if not PRIVATE_KEY:
        return ""
    return Account.from_key(PRIVATE_KEY).address


def receipt_position(tx_hash, token, owner):
    if not tx_hash:
        return 0
    try:
        receipt = call_rpc("eth_getTransactionReceipt", [tx_hash])
        for log in (receipt or {}).get("logs", []):
            topics = log.get("topics") or []
            if len(topics) < 3:
                continue
            if topics[0].lower() != "0x" + BUY_EVENT:
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


def check():
    try:
        state = json.loads(PATH.read_text())
    except Exception:
        state = {"bought": []}

    owner = owner_address()
    if not owner:
        return {
            "status": "waiting_signer",
            "auto_exit_live": AUTO_EXIT_LIVE,
        }

    rows = []
    for item in state.get("bought", []):
        token = item.get("token", "")
        if not token:
            continue

        row = {
            "token": token,
            "cost_usdc": item.get("amount_usdc"),
            "buy_tx": item.get("tx_hash", ""),
            "claim_selector": "0x" + CLAIM,
            "sell_selector": "0x" + SELL,
        }

        try:
            launch = words(rpc_call("0x" + STATE + word_address(token)))
            pair_words = words(
                rpc_call("0x" + PAIR + word_address(token) + word_address(USDC))
            )
            pair = (
                "0x" + f"{pair_words[0]:040x}"
                if pair_words
                else "0x" + "0" * 40
            )
            graduated = bool(
                len(launch) >= 6
                and launch[1] == 1
                and launch[5] != 0
                and int(pair, 16) != 0
            )
            balance_words = words(
                rpc_call("0x" + BALANCE + word_address(owner), token)
            )
            wallet_balance = balance_words[0] if balance_words else 0
            position = receipt_position(item.get("tx_hash", ""), token, owner)

            if graduated and wallet_balance > 0:
                position_status = "wallet_ready_for_sell_quote"
            elif graduated and position > 0:
                position_status = "graduated_bonded_exit_needs_verified_unbond"
            elif not graduated:
                position_status = "waiting_graduation"
            else:
                position_status = "waiting_unbond_or_claim"

            row.update(
                {
                    "graduated": graduated,
                    "pair": pair,
                    "wallet_token_balance": str(wallet_balance),
                    "bonded_token_amount": str(position),
                    "status": position_status,
                }
            )
        except Exception as exc:
            row.update({"status": "read_error", "error": str(exc)})

        rows.append(row)

    return {
        "status": "ok",
        "owner": owner,
        "positions": rows,
        "auto_exit_live": AUTO_EXIT_LIVE,
        "sell_loop": "fail_closed_until_unbond_and_net_quote_verified",
    }
