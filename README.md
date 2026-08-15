# BUNKER

Soft Hub patch for [thebunkerhood.com](https://thebunkerhood.com).

## Install

```text
dist/bunker-waitlist-1.1.3.softhub.zip
```

Soft Hub → Patches → drop the zip → Prepare if needed.

## Actions

| Action | What it does |
|---|---|
| Register | Join waitlist via account proxy |
| Inspect | Read site + collection status |
| Watch | Poll the same snapshot until deadline |

## Build

```bash
python3 scripts/build_plugin.py \
  bunker-waitlist \
  dist/bunker-waitlist-1.1.3.softhub.zip
```
