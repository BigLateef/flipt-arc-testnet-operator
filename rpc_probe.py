#!/usr/bin/env python3
import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent

def load_env():
    values = {}
    p = ROOT / '.env'
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            values[k.strip()] = v.strip()
    return values

def rpc(url, method, params=None):
    body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or []}).encode()
    req = urllib.request.Request(url, body, {'Content-Type': 'application/json', 'User-Agent': 'flipt-termux-operator/0.1'})
    with urllib.request.urlopen(req, timeout=20) as response:
        payload = json.loads(response.read().decode())
    if 'error' in payload:
        raise RuntimeError(payload['error'])
    return payload['result']

def main():
    env = load_env()
    url = env.get('ARC_RPC_URL', 'https://rpc.testnet.arc.io')
    expected = int(env.get('CHAIN_ID', '5042002'))
    chain_hex = rpc(url, 'eth_chainId')
    block_hex = rpc(url, 'eth_blockNumber')
    chain_id = int(chain_hex, 16)
    block = int(block_hex, 16)
    print(json.dumps({
        'rpc_url': url,
        'chain_id': chain_id,
        'expected_chain_id': expected,
        'chain_match': chain_id == expected,
        'latest_block': block,
        'read_only': True,
    }, indent=2))
    if chain_id != expected:
        sys.exit(2)

if __name__ == '__main__':
    main()
