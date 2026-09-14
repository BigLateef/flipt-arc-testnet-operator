# Flipt Termux Operator (safe bootstrap)

Local, wallet-safe bootstrap for the Flipt.fun Arc Testnet workflow.

## What this milestone does

- Runs entirely on the Samsung/Termux device.
- Uses the official Arc Testnet RPC: `https://rpc.testnet.arc.io`.
- Serves a local dashboard that connects to Rabby's injected wallet provider.
- Verifies wallet address, wallet chain ID, RPC chain ID, latest block, and native USDC balance.
- Does **not** create, buy, sell, approve, sign, or broadcast transactions.
- Does not store seed phrases or private keys.

## Termux setup

```sh
pkg update -y
pkg install python -y
cd ~/flipt-termux-operator
cp .env.example .env
python3 rpc_probe.py
python3 server.py
```

Keep the server running. Open this URL inside Rabby's in-app browser on the same phone:

```text
http://127.0.0.1:8787
```

Tap **Connect Rabby**. The dashboard must show:

- chain ID `5042002`
- RPC chain ID `5042002`
- RPC URL `https://rpc.testnet.arc.io`
- a wallet address
- a native USDC balance

If the values do not match, stop. Do not sign anything.

## Safety switches

`.env` starts in safe mode:

```text
DRY_RUN=1
LIVE_TRADING=0
```

Those switches remain required for later milestones. Transaction code will not be enabled in this bootstrap.

## Render free-tier deployment

`render.yaml` uses one Free Web Service. Render spins a Free Web Service down after 15 minutes without inbound traffic, so create a cron-job.org GET job that calls `/tick` every 1–5 minutes. Set the same secret in the URL query or `X-Cron-Secret` header.

1. Put this folder in a separate GitHub repository.
2. In Render, create a Blueprint from that repository.
3. Set `CRON_SECRET` to a long random value and set `RUN_END_AT` to the real testnet deadline.
4. Create a cron-job.org GET request to the deployed service's `/tick` path every minute or five minutes, passing the configured secret.
5. Confirm `/health` returns `chain_match: true` and `/status` returns the latest block.

The cron tick is the monitor trigger; there is no separate Free Background Worker. `/execute` is a separate secret-protected, one-call endpoint for the configured first buy. Do not attach `/execute` to the recurring cron job. The service must persist state in Postgres before we rely on it for event history, because Render Free filesystems are ephemeral and services can restart or spin down.

Verified Arc testnet addresses and high-confidence selectors are recorded in `contracts.json`. The buy executor requires a positive `MIN_TOKENS_OUT`, performs exact USDC approval only, checks chain/balance/gas/caps, and remains fail-closed when live switches or burner configuration are absent. Launch, unbond, and limit-order creation remain blocked until their exact ABI tuples are independently verified.

## Next milestone

After the read-only worker is verified on Render, the next step is to add Flipt launch discovery and a persistent ledger. Transaction preparation will be added only after the live contract addresses and interfaces are verified from the app/RPC; no guessed contract addresses are allowed.
