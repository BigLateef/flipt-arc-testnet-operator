#!/usr/bin/env python3
"""Fail-closed Arc Testnet executor for one configured Flipt curve buy.

This module never reads a key from the repository. The key must be provided as
BURNER_PRIVATE_KEY in the Render secret environment. It supports one explicit
buy call at a time; the web layer must not call it on every health tick.
"""
import json
import os
import time
import urllib.request
from decimal import Decimal

from eth_account import Account

RPC_URL = os.getenv('ARC_RPC_URL', 'https://rpc.testnet.arc.io')
CHAIN_ID = int(os.getenv('CHAIN_ID', '5042002'))
HUB = '0x4B33146F2bCc75574534374C85662f9E51C38Aca'
# Arc Testnet uses native USDC at 0x3600... for balances and approvals.
USDC = os.getenv('USDC_ADDRESS', '0x3600000000000000000000000000000000000000')
DRY_RUN = os.getenv('DRY_RUN', '1') == '1'
LIVE_TRADING = os.getenv('LIVE_TRADING', '0') == '1'
MAX_USDC_PER_LAUNCH = Decimal(os.getenv('MAX_USDC_PER_LAUNCH', '100'))
TARGET = os.getenv('TARGET_LAUNCH_ADDRESS', '').strip()
BUY_USDC_AMOUNT = Decimal(os.getenv('BUY_USDC_AMOUNT', '0'))
MIN_TOKENS_OUT = int(os.getenv('MIN_TOKENS_OUT', '0'))
PRIVATE_KEY = os.getenv('BURNER_PRIVATE_KEY', '').strip()

BUY_SELECTOR = 'a59ac6dd'
APPROVE_SELECTOR = '095ea7b3'
BALANCE_OF_SELECTOR = '70a08231'
ALLOWANCE_SELECTOR = 'dd62ed3e'


def word_address(address):
    a = address.lower().removeprefix('0x')
    if len(a) != 40 or any(c not in '0123456789abcdef' for c in a):
        raise ValueError('invalid address')
    return a.rjust(64, '0')


def word_uint(value):
    if int(value) < 0:
        raise ValueError('negative uint')
    return f'{int(value):064x}'


def call_rpc(method, params=None):
    body = json.dumps({'jsonrpc': '2.0', 'id': int(time.time() * 1000), 'method': method, 'params': params or []}).encode()
    request = urllib.request.Request(RPC_URL, body, {'Content-Type': 'application/json', 'User-Agent': 'flipt-arc-testnet-operator/0.2'})
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode())
    if 'error' in payload:
        raise RuntimeError(str(payload['error']))
    return payload['result']


def eth_call(to, data, from_address=None):
    tx = {'to': to, 'data': data}
    if from_address:
        tx['from'] = from_address
    return call_rpc('eth_call', [tx, 'latest'])


def read_uint(to, data, from_address=None):
    result = eth_call(to, data, from_address)
    return int(result[2:], 16) if result and result != '0x' else 0


def make_tx(account, to, data, nonce, gas=None, gas_price=None):
    if gas_price is None:
        gas_price = int(call_rpc('eth_gasPrice'), 16)
    if gas is None:
        gas = int(call_rpc('eth_estimateGas', [{'from': account, 'to': to, 'data': data, 'value': '0x0'}]), 16)
        gas = int(gas * 1.15)
    return {
        'chainId': CHAIN_ID,
        'nonce': nonce,
        'to': to,
        'value': 0,
        'gas': gas,
        'gasPrice': gas_price,
        'data': data,
    }


def sign_and_send(tx):
    signed = Account.sign_transaction(tx, PRIVATE_KEY)
    raw = getattr(signed, 'raw_transaction', None)
    if raw is None:
        raw = signed.rawTransaction
    return call_rpc('eth_sendRawTransaction', ['0x' + raw.hex()])


def execute_buy():
    if DRY_RUN or not LIVE_TRADING:
        return {'ok': False, 'status': 'blocked', 'reason': 'live switches are not enabled'}
    if not PRIVATE_KEY:
        return {'ok': False, 'status': 'blocked', 'reason': 'BURNER_PRIVATE_KEY is not configured'}
    if not TARGET:
        return {'ok': False, 'status': 'blocked', 'reason': 'TARGET_LAUNCH_ADDRESS is not configured'}
    if BUY_USDC_AMOUNT <= 0 or BUY_USDC_AMOUNT > MAX_USDC_PER_LAUNCH:
        return {'ok': False, 'status': 'blocked', 'reason': 'BUY_USDC_AMOUNT is outside the configured cap'}
    if MIN_TOKENS_OUT <= 0:
        return {'ok': False, 'status': 'blocked', 'reason': 'MIN_TOKENS_OUT must be positive; no zero-slippage bypass'}

    chain = int(call_rpc('eth_chainId'), 16)
    if chain != CHAIN_ID:
        return {'ok': False, 'status': 'blocked', 'reason': f'chain mismatch: {chain}'}

    account = Account.from_key(PRIVATE_KEY)
    owner = account.address
    native_balance = int(call_rpc('eth_getBalance', [owner, 'latest']), 16)
    usdc_amount = int(BUY_USDC_AMOUNT * Decimal(10**6))
    usdc_balance = read_uint(USDC, '0x' + BALANCE_OF_SELECTOR + word_address(owner))
    if usdc_balance < usdc_amount:
        return {'ok': False, 'status': 'blocked', 'reason': 'insufficient USDC balance', 'wallet': owner, 'usdc_balance': usdc_balance}
    if native_balance <= 0:
        return {'ok': False, 'status': 'blocked', 'reason': 'no native USDC gas balance', 'wallet': owner}

    allowance = read_uint(USDC, '0x' + ALLOWANCE_SELECTOR + word_address(owner) + word_address(HUB))
    nonce = int(call_rpc('eth_getTransactionCount', [owner, 'pending']), 16)
    if allowance < usdc_amount:
        # Exact approval only; no unlimited approval.
        approval_data = '0x' + APPROVE_SELECTOR + word_address(HUB) + word_uint(usdc_amount)
        tx = make_tx(owner, USDC, approval_data, nonce)
        if DRY_RUN:
            return {'ok': True, 'status': 'approval_prepared', 'tx': tx, 'wallet': owner}
        tx_hash = sign_and_send(tx)
        return {'ok': True, 'status': 'approval_broadcast', 'tx_hash': tx_hash, 'wallet': owner, 'amount_usdc': str(BUY_USDC_AMOUNT)}

    # buy(address,uint256,uint256): launch, exact USDC input, minimum token output.
    buy_data = '0x' + BUY_SELECTOR + word_address(TARGET) + word_uint(usdc_amount) + word_uint(MIN_TOKENS_OUT)
    tx = make_tx(owner, HUB, buy_data, nonce)
    if DRY_RUN:
        return {'ok': True, 'status': 'buy_prepared', 'tx': tx, 'wallet': owner}
    tx_hash = sign_and_send(tx)
    return {'ok': True, 'status': 'buy_broadcast', 'tx_hash': tx_hash, 'wallet': owner, 'amount_usdc': str(BUY_USDC_AMOUNT), 'target': TARGET}
