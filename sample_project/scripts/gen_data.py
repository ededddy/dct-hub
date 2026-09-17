"""Generate deterministic sample sales data for the demo dbt Charts project.

Usage:
    python scripts/gen_data.py                  # today is ~60% complete (intraday)
    python scripts/gen_data.py --complete-today # regenerate with today at 100%

Re-running with --complete-today simulates an intraday report filling in:
same rows for all past days (frozen), more revenue/orders for today.
"""

import argparse
import csv
import random
from datetime import date, timedelta
from pathlib import Path

DAYS = 120
OUT = Path(__file__).resolve().parent.parent / "data" / "sales.csv"

REGIONS = ["East", "West", "Central"]
PRODUCTS = ["Enterprise", "Team", "Starter"]

# (base_revenue, base_orders) per product
PRODUCT_BASE = {
    "Enterprise": (9000.0, 3),
    "Team": (3200.0, 12),
    "Starter": (1500.0, 20),
}
REGION_FACTOR = {"East": 1.15, "West": 1.0, "Central": 0.8}


def rows_for(day: date, rng: random.Random, completeness: float) -> list[list]:
    rows = []
    # gentle upward trend + weekday seasonality
    age = (date.today() - day).days
    trend = 1.0 + (DAYS - age) * 0.0015
    weekday = 0.75 if day.weekday() >= 5 else 1.0 + 0.08 * (day.weekday() % 3)
    for region in REGIONS:
        for product in PRODUCTS:
            base_rev, base_orders = PRODUCT_BASE[product]
            factor = REGION_FACTOR[region] * trend * weekday * completeness
            orders = max(1, round(base_orders * factor * rng.uniform(0.85, 1.15)))
            avg = base_rev / base_orders * rng.uniform(0.9, 1.1)
            rows.append([day.isoformat(), region, product, round(orders * avg * factor, 2), orders])
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--complete-today", action="store_true")
    args = parser.parse_args()

    rng = random.Random(42)
    today = date.today()
    all_rows = []
    for i in range(DAYS, -1, -1):
        day = today - timedelta(days=i)
        completeness = 1.0
        if day == today and not args.complete_today:
            completeness = 0.6
            rng_today = random.Random(4242)  # stable partial-day noise
            all_rows.extend(rows_for(day, rng_today, completeness))
            continue
        all_rows.extend(rows_for(day, rng, completeness))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "region", "product", "revenue", "orders"])
        writer.writerows(all_rows)
    print(f"wrote {len(all_rows)} rows to {OUT}")


if __name__ == "__main__":
    main()
