"""Print the local, privacy-safe Overlayr PMF funnel summary."""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))


def main() -> None:
    from overlayr.services.product_analytics import ProductAnalytics

    analytics = ProductAnalytics.for_default_location()
    print(json.dumps(analytics.summary(), indent=2))


if __name__ == "__main__":
    main()
