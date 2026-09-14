#!/usr/bin/env python3
import json
import os
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RPC_URL = os.getenv('ARC_RPC_URL', 'https://rpc.testnet.arc.io')
EXPECTED_CHAIN_ID = int(os.getenv('CHAIN_ID', '5042002'))
DRY_RUN = os.getenv('DRY_RUN', '1')
LIVE_TRADING = os.getenv('LIVE_TRADING', '0')
RUN_END_AT = os.getenv('RUN_END_AT', '')
CRON_SECRET = os.getenv('CRON_SECRET', '')
PORT = int(os.getenv('PORT', '10000'))


def rpc(method, params=None):
    body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or []}).encode()
    req = urllib.request.Request(RPC_URL, body, {'Content-Type': 'application/json', 'User-Agent': 'flipt-render-bootstrap/0.1'})
    with urllib.request.urlopen(req, timeout=15) as response:
        payload = json.loads(response.read().decode())
    if 'error' in payload:
        raise RuntimeError(str(payload['error']))
    return payload['result']


def status():
    chain = int(rpc('eth_chainId'), 16)
    block = int(rpc('eth_blockNumber'), 16)
    return {
        'service': 'flipt-render-dashboard',
        'read_only': True,
        'rpc_url': RPC_URL,
        'rpc_chain_id': chain,
        'expected_chain_id': EXPECTED_CHAIN_ID,
        'chain_match': chain == EXPECTED_CHAIN_ID,
        'latest_block': block,
        'dry_run': DRY_RUN,
        'live_trading': LIVE_TRADING,
        'run_end_at': RUN_END_AT or None,
        'timestamp': int(time.time()),
    }


class Handler(BaseHTTPRequestHandler):
    def send_json(self, value, code=200):
        data = json.dumps(value).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path == '/health':
            try:
                self.send_json({'ok': True, 'read_only': True, 'chain_match': status()['chain_match']})
            except Exception as exc:
                self.send_json({'ok': False, 'error': str(exc)}, 503)
            return
        if parsed.path == '/tick':
            supplied = self.headers.get('X-Cron-Secret', '') or (query.get('secret') or [''])[0]
            if not CRON_SECRET or supplied != CRON_SECRET:
                self.send_json({'ok': False, 'error': 'unauthorized'}, 401)
                return
            try:
                self.send_json({'ok': True, 'tick': status(), 'read_only': True})
            except Exception as exc:
                self.send_json({'ok': False, 'error': str(exc)}, 503)
            return
        if parsed.path == '/status':
            try:
                self.send_json(status())
            except Exception as exc:
                self.send_json({'ok': False, 'error': str(exc)}, 503)
            return
        if self.path == '/':
            body = '''<!doctype html><meta name="viewport" content="width=device-width"><title>Flipt Render</title><h1>Flipt Render bootstrap</h1><p>Read-only worker dashboard.</p><p><a href="/health">Health</a> · <a href="/status">Status</a></p><p>DRY_RUN=1 · LIVE_TRADING=0</p>'''.encode()
            self.send_response(200); self.send_header('Content-Type','text/html'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body); return
        self.send_error(404)

    def log_message(self, fmt, *args):
        print(fmt % args, flush=True)


if __name__ == '__main__':
    print(f'Flipt Render dashboard listening on {PORT}', flush=True)
    ThreadingHTTPServer(('0.0.0.0', PORT), Handler).serve_forever()
