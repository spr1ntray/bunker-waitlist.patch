# BUNKER

Soft Hub patch for [thebunkerhood.com](https://thebunkerhood.com).

## Install

```text
dist/bunker-waitlist-1.2.5.softhub.zip
```

Soft Hub → Patches → drop the zip → Prepare if needed.

## Actions

| Action | What it does |
|---|---|
| Register | Join waitlist via account proxy |
| WL mint + dump | Check wallets, wait for allowlist, mint WL, list first |
| WL mint + fixed | Check wallets, wait for allowlist, mint WL, list at a fixed ETH price |
| WL mint + percent | Check wallets, wait for allowlist, mint WL, list at mint cost + percent |

## Build

```bash
python3 scripts/build_plugin.py \
  bunker-waitlist \
  dist/bunker-waitlist-1.2.5.softhub.zip
```
