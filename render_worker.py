#!/usr/bin/env python3
import json
import os
import time
import urllib.request
from datetime import datetime, timezone

RPC_URL = os.getenv('ARC_RPC_URL', 'https://rpc.testnet.arc.io')
EXPECTED_CHAIN_ID = int(os.getenv('CHAIN_ID', '5042002'))
DRY_RUN = os.getenv('DRY_RUN', '1')
LIVE_TRADING = os.getenv('LIVE_TRADING', '0')
RUN_END_AT = os.getenv('RUN_END_AT', '')


def rpc(method, params=None):
    body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or []}).encode()
    req = urllib.request.Request(RPC_URL, body, {'Content-Type': 'application/json', 'User-Agent': 'flipt-render-worker/0.1'})
    with urllib.request.urlopen(req, timeout=15) as response:
        payload = json.loads(response.read().decode())
    if 'error' in payload:
        raise RuntimeError(str(payload['error']))
    return payload['result']


def main():
    print(json.dumps({'event': 'worker_start', 'rpc_url': RPC_URL, 'expected_chain_id': EXPECTED_CHAIN_ID, 'dry_run': DRY_RUN, 'live_trading': LIVE_TRADING, 'run_end_at': RUN_END_AT}), flush=True)
    while True:
        try:
            chain = int(rpc('eth_chainId'), 16)
            block = int(rpc('eth_blockNumber'), 16)
            row = {
                'event': 'rpc_heartbeat',
                'time': datetime.now(timezone.utc).isoformat(),
                'chain_id': chain,
                'expected_chain_id': EXPECTED_CHAIN_ID,
                'chain_match': chain == EXPECTED_CHAIN_ID,
                'latest_block': block,
                'read_only': True,
                'dry_run': DRY_RUN,
            }
            print(json.dumps(row), flush=True)
            if chain != EXPECTED_CHAIN_ID:
                print(json.dumps({'event': 'halt', 'reason': 'chain_id_mismatch'}), flush=True)
                time.sleep(30)
                continue
        except Exception as exc:
            print(json.dumps({'event': 'rpc_error', 'error': str(exc)}), flush=True)
        time.sleep(15)


if __name__ == '__main__':
    main()
