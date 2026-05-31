"""Dev helper: fetch ONE pin anonymously and print the raw JSON.

Usage:
    python scripts/dump_pin.py "<pin url | pin.it link | pin id>"

This is a Step 3 verification tool to confirm true Pinterest field paths before
we write the defensive mapper in Step 4. Anonymous mode (no Pinterest cookie).
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# Allow running as `python scripts/dump_pin.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.pinterest import pin as pin_mod  # noqa: E402
from app.pinterest.client import BlockedError, get_client  # noqa: E402


async def main(pin_input: str) -> int:
    client = get_client()
    try:
        pin_id = await pin_mod.resolve_pin_id(pin_input, client)
        print(f"# resolved pin id: {pin_id}", file=sys.stderr)
        raw = await pin_mod.fetch_pin_raw(pin_input, client)
    except pin_mod.PinUrlError as exc:
        print(f"INPUT ERROR: {exc}", file=sys.stderr)
        return 2
    except BlockedError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 3
    finally:
        await client.aclose()

    print(json.dumps(raw, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        sys.exit(1)
    sys.exit(asyncio.run(main(sys.argv[1])))
