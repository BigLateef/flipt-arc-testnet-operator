#!/usr/bin/env python3
import json
import os
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WEB = ROOT / 'web'

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

ENV = load_env()
RPC_URL = ENV.get('ARC_RPC_URL', 'https://rpc.testnet.arc.io')
EXPECTED_CHAIN_ID = int(ENV.get('CHAIN_ID', '5042002'))
HOST = ENV.get('HOST', '127.0.0.1')
PORT = int(ENV.get('PORT', '8787'))

def rpc(method, params=None):
    body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or []}).encode()
    req = urllib.request.Request(RPC_URL, body, {'Content-Type': 'application/json', 'User-Agent': 'flipt-termux-operator/0.1'})
    with urllib.request.urlopen(req, timeout=15) as response:
        payload = json.loads(response.read().decode())
    if 'error' in payload:
        raise RuntimeError(str(payload['error']))
    return payload['result']

class Handler(BaseHTTPRequestHandler):
    def send_json(self, payload, status=200):
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == '/api/health':
            try:
                chain_hex = rpc('eth_chainId')
                block_hex = rpc('eth_blockNumber')
                chain_id = int(chain_hex, 16)
                self.send_json({
                    'ok': True,
                    'read_only': True,
                    'rpc_url': RPC_URL,
                    'rpc_chain_id': chain_id,
                    'expected_chain_id': EXPECTED_CHAIN_ID,
                    'chain_match': chain_id == EXPECTED_CHAIN_ID,
                    'latest_block': int(block_hex, 16),
                    'dry_run': ENV.get('DRY_RUN', '1'),
                    'live_trading': ENV.get('LIVE_TRADING', '0'),
                })
            except Exception as exc:
                self.send_json({'ok': False, 'read_only': True, 'error': str(exc)}, 502)
            return

        path = self.path.split('?', 1)[0]
        if path == '/':
            path = '/index.html'
        target = (WEB / path.lstrip('/')).resolve()
        if WEB not in target.parents or not target.is_file():
            self.send_error(404)
            return
        data = target.read_bytes()
        content_type = 'text/html; charset=utf-8' if target.suffix == '.html' else 'text/plain; charset=utf-8'
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        print('%s - %s' % (self.address_string(), fmt % args))

if __name__ == '__main__':
    print(f'Flipt Termux Operator read-only dashboard: http://{HOST}:{PORT}')
    print(f'RPC: {RPC_URL} | expected chain: {EXPECTED_CHAIN_ID}')
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
