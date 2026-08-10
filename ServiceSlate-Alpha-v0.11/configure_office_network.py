from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

from serviceslate.db import DATA_DIR

CONFIG = DATA_DIR / ".lan-config.json"


def save(payload: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Saved: {CONFIG}")


def main() -> None:
    print("ServiceSlate Office Network")
    print("1) This computer is the Office Host")
    print("2) Join an existing Office Host")
    print("3) Standalone / local only")
    choice = input("Choose 1, 2, or 3: ").strip()
    if choice == "1":
        save({"role": "host", "port": 8765})
        print("This computer will share ServiceSlate with authenticated users on the same private network.")
        print("If Windows asks about network access, allow ServiceSlate/Python on Private networks only.")
    elif choice == "2":
        raw = input("Office Host address (example http://192.168.1.20:8765): ").strip().rstrip("/")
        parsed = urlparse(raw)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise SystemExit("That does not look like a valid ServiceSlate host address.")
        save({"role": "client", "host_url": raw})
        print("This computer will use the Office Host as the authoritative ServiceSlate data source.")
    elif choice == "3":
        save({"role": "local"})
        print("ServiceSlate will run locally on this computer only.")
    else:
        raise SystemExit("No change made.")


if __name__ == "__main__":
    main()
