import json, os
from decimal import Decimal
from pathlib import Path
from live_executor import HUB, USDC, call_rpc, word_address, word_uint

STATE='029282d7'; PAIR='e6a43905'; BALANCE='70a08231'
PATH=Path(os.getenv('AUTO_STATE_PATH','auto_strategy_state.json'))

def rpc_call(data,to=HUB):
    return call_rpc('eth_call',[{'to':to,'data':data},'latest'])

def words(value):
    raw=(value or '0x')[2:]
    return [int(raw[i:i+64],16) for i in range(0,len(raw),64)] if raw and len(raw)%64==0 else []

def check():
    try: state=json.loads(PATH.read_text())
    except Exception: state={'bought':[]}
    owner=os.getenv('BURNER_ADDRESS','').strip()
    if not owner:
        return {'status':'waiting_signer_address'}
    rows=[]
    for item in state.get('bought',[]):
        token=item.get('token','')
        if not token: continue
        v=words(rpc_call('0x'+STATE+word_address(token)))
        pair_words=words(rpc_call('0x'+PAIR+word_address(token)+word_address(USDC)))
        pair='0x'+f'{pair_words[0]:040x}' if pair_words else '0x'+'0'*40
        graduated=bool(len(v)>=6 and v[1]==1 and v[5]!=0 and int(pair,16)!=0)
        balance=0
        if owner:
            balance=words(rpc_call('0x'+BALANCE+word_address(owner),token))[0] if words(rpc_call('0x'+BALANCE+word_address(owner),token)) else 0
        rows.append({'token':token,'cost_usdc':item.get('amount_usdc','25'),'graduated':graduated,'pair':pair,'wallet_token_balance':str(balance),'status':'ready_for_quote' if graduated and balance>0 else ('waiting_graduation' if not graduated else 'waiting_unbond_or_claim')})
    return {'status':'ok','positions':rows,'sell_loop':'gated_until_verified_quote'}

