#!/usr/bin/env python3
"""Guarded Flipt launch discovery and capped buy loop.

The loop is intentionally buy-only. It discovers launches from the verified hub
read methods, skips the already-traded target, uses a positive minTokensOut
computed from current on-chain curve state with a 10% safety haircut, and
requires gas estimation to pass before signing. Sell/unbond remains separate
until its position lifecycle is independently verified.
"""
import json
import os
import time
from decimal import Decimal
from pathlib import Path

from eth_account import Account

from live_executor import (
    BUY_USDC_AMOUNT,
    CHAIN_ID,
    DRY_RUN,
    HUB,
    LIVE_TRADING,
    MAX_USDC_PER_LAUNCH,
    PRIVATE_KEY,
    RPC_URL,
    USDC,
    call_rpc,
    make_tx,
    read_uint,
    sign_and_send,
    word_address,
    word_uint,
)

LAUNCH_COUNT_SELECTOR = '27cca59f'
LAUNCHES_SELECTOR = '7b443a76'
LAUNCH_OF_SELECTOR = '029282d7'
MIN_FDV_USD = Decimal(os.getenv('AUTO_MIN_FDV_USD', '2000'))
INITIAL_FDV_USD = Decimal('2100')
TOKEN_SUPPLY = 10**9 * 10**18
MAX_AUTO_BUYS = int(os.getenv('MAX_AUTO_BUYS', '3'))
AUTO_BUY_AMOUNT = Decimal(os.getenv('AUTO_BUY_USDC_AMOUNT', str(BUY_USDC_AMOUNT or '25')))
STATE_PATH = Path(os.getenv('AUTO_STATE_PATH', 'auto_strategy_state.json'))
END_AT = os.getenv('RUN_END_AT', '2026-09-16T02:00:00+01:00')


def selector_call(to, selector, arg=None):
    data = '0x' + selector + (word_uint(arg) if arg is not None else '')
    return call_rpc('eth_call', [{'to': to, 'data': data}, 'latest'])


def decode_words(result):
    raw = (result or '0x')[2:]
    if len(raw) % 64 or not raw:
        return []
    return [int(raw[i:i + 64], 16) for i in range(0, len(raw), 64)]


def load_state():
    try:
        value = json.loads(STATE_PATH.read_text())
        if isinstance(value, dict):
            return value
    except Exception:
        pass
    return {'seen': [], 'bought': []}


def save_state(state):
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True))


def parse_end():
    try:
        from datetime import datetime
        return datetime.fromisoformat(END_AT).timestamp()
    except Exception:
        return 0


def discover(limit=80):
    count_result = selector_call(HUB, LAUNCH_COUNT_SELECTOR)
    words = decode_words(count_result)
    count = words[0] if words else 0
    state = load_state()
    seen = {x.lower() for x in state.get('seen', [])}
    launches = []
    start = max(0, count - limit)
    for index in range(count - 1, start - 1, -1):
        token_result = selector_call(HUB, LAUNCHES_SELECTOR, index)
        token_words = decode_words(token_result)
        if not token_words:
            continue
        token = '0x' + f'{token_words[0]:040x}'
        token_key = token.lower()
        if token_key in seen:
            continue
        state['seen'] = list(seen | {token_key})[-500:]
        launch_result = selector_call(HUB, LAUNCH_OF_SELECTOR, int(token[2:], 16))
        values = decode_words(launch_result)
        if len(values) < 9 or values[0] == 0:
            continue
        graduated = values[1] == 1 and values[5] != 0
        if graduated:
            continue
        raised_usdc = Decimal(values[2]) / Decimal(10**6)
        sold_tokens = Decimal(values[3]) / Decimal(10**18)
        # The app's verified launch floor is about $2.1K. Once tokens are
        # sold, average curve price gives a conservative FDV estimate.
        if sold_tokens > 0 and raised_usdc > 0:
            fdv = (raised_usdc * Decimal(10**9)) / sold_tokens
        else:
            fdv = INITIAL_FDV_USD
        launches.append({
            'token': token,
            'fdv_usd': str(fdv.quantize(Decimal('0.01'))),
            'raised_usdc': str(raised_usdc),
            'sold_tokens': str(sold_tokens),
            'graduated': False,
            'index': index,
        })
    save_state(state)
    return launches, state


def min_tokens_out(launch):
    raised = Decimal(launch['raised_usdc'])
    sold = Decimal(launch['sold_tokens'])
    amount_raw = int(AUTO_BUY_AMOUNT * Decimal(10**6))
    if raised > 0 and sold > 0:
        expected = Decimal(amount_raw) * (sold * Decimal(10**18)) / (raised * Decimal(10**6))
    else:
        expected = (AUTO_BUY_AMOUNT / INITIAL_FDV_USD) * Decimal(TOKEN_SUPPLY)
    # Positive, nonzero 10% safety haircut. No zero-slippage bypass.
    return max(1, int(expected * Decimal('0.90')))


def execute_auto_buy(launch):
    if DRY_RUN or not LIVE_TRADING:
        return {'ok': False, 'status': 'blocked', 'reason': 'live switches are not enabled'}
    if not PRIVATE_KEY:
        return {'ok': False, 'status': 'blocked', 'reason': 'burner is not configured'}
    if AUTO_BUY_AMOUNT <= 0 or AUTO_BUY_AMOUNT > MAX_USDC_PER_LAUNCH:
        return {'ok': False, 'status': 'blocked', 'reason': 'auto amount exceeds cap'}
    account = Account.from_key(PRIVATE_KEY)
    owner = account.address
    amount_raw = int(AUTO_BUY_AMOUNT * Decimal(10**6))
    balance = read_uint(USDC, '0x70a08231' + word_address(owner))
    if balance < amount_raw:
        return {'ok': False, 'status': 'blocked', 'reason': 'insufficient Flipt ERC-20 USDC', 'wallet': owner, 'usdc_balance': balance}
    allowance = read_uint(USDC, '0xdd62ed3e' + word_address(owner) + word_address(HUB))
    nonce = int(call_rpc('eth_getTransactionCount', [owner, 'pending']), 16)
    if allowance < amount_raw:
        approval = '0x095ea7b3' + word_address(HUB) + word_uint(amount_raw)
        tx = make_tx(owner, USDC, approval, nonce)
        if DRY_RUN:
            return {'ok': True, 'status': 'approval_prepared', 'wallet': owner, 'token': launch['token']}
        tx_hash = sign_and_send(tx)
        return {'ok': True, 'status': 'approval_broadcast', 'tx_hash': tx_hash, 'wallet': owner, 'token': launch['token']}
    min_out = min_tokens_out(launch)
    buy_data = '0xa59ac6dd' + word_address(launch['token']) + word_uint(amount_raw) + word_uint(min_out)
    tx = make_tx(owner, HUB, buy_data, nonce)
    if DRY_RUN:
        return {'ok': True, 'status': 'buy_prepared', 'wallet': owner, 'token': launch['token'], 'min_tokens_out': min_out}
    tx_hash = sign_and_send(tx)
    return {'ok': True, 'status': 'buy_broadcast', 'tx_hash': tx_hash, 'wallet': owner, 'token': launch['token'], 'amount_usdc': str(AUTO_BUY_AMOUNT), 'min_tokens_out': min_out}


def run_once():
    if parse_end() and time.time() >= parse_end():
        return {'ok': True, 'status': 'ended', 'reason': 'campaign end reached'}
    launches, state = discover()
    skip = {os.getenv('TARGET_LAUNCH_ADDRESS', '').lower()}
    bought = {x.get('token', '').lower() for x in state.get('bought', [])}
    candidates = [x for x in launches if x['token'].lower() not in skip and x['token'].lower() not in bought and Decimal(x['fdv_usd']) >= MIN_FDV_USD]
    if len(bought) >= MAX_AUTO_BUYS:
        return {'ok': True, 'status': 'cap_reached', 'bought_count': len(bought), 'candidates': len(candidates)}
    if not candidates:
        return {'ok': True, 'status': 'no_candidate', 'discovered': len(launches), 'bought_count': len(bought)}
    launch = candidates[0]
    result = execute_auto_buy(launch)
    if result.get('status') == 'buy_broadcast':
        state.setdefault('bought', []).append({**launch, **result, 'time': int(time.time())})
        save_state(state)
    return {'ok': result.get('ok', False), 'status': result.get('status'), 'launch': launch, 'result': result}

