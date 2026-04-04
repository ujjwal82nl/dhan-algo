# Strategy: Option Selling with Adjustments (`shortStrangle_Adjust`)

---

## Config — Global (`config.py`)

```python
INSTRUMENTS = {
    "NIFTY": "INDEX"
}

# Option Chain
EXPIRY_INDEX = 0       # 0 = current/nearest expiry
NUM_STRIKES  = 30      # strikes either side of ATM to fetch

# Strategy Parameters
TARGET_DELTA         = 0.18   # sell strikes near this delta
MAX_DELTA            = 0.20   # reject strike if delta exceeds this
MIN_PREMIUM          = 50     # minimum combined premium to enter (Rs.)
PROFIT_TARGET_PCT    = 0.70   # close at 70% of premium collected
STOP_LOSS_MULTIPLIER = 1.5    # close if loss exceeds 1.5× premium collected

# Position Sizing
MAX_LOTS_PER_TRADE   = 2
MAX_OPEN_POSITIONS   = 6
MAX_DAILY_LOSS_INR   = 10000
```

## Config — Strategy Level (`strategy_shortStrangle_Adjust.py`)

```python
ADJUST_THRESHOLD = 0.40   # adjustment triggers when imbalance > 40% of total credit
```

---

## Entry Rules

- **Friday skip:** No new entry on Friday (NIFTY expires Tuesday — too close to expiry)
- **Already in:** Skip if an open position already exists for this instrument
- **Strike selection:** Find CE and PE strikes with delta nearest `TARGET_DELTA`, pick highest OI on each side
- **Premium floor:** Combined `CE_LTP + PE_LTP` must be ≥ `MIN_PREMIUM`

---

## Adjustment Rules

**Trigger condition** (all values in total Rs., including quantity):

```
imbalance_Rs      = |ce_ltp - pe_ltp| × quantity
adj_threshold_Rs  = ADJUST_THRESHOLD × total_premium_collected

Trigger when: imbalance_Rs > adj_threshold_Rs
```

> Note: `total_premium_collected` is the sum of `entry_premium` across all currently
> open legs (already in total Rs.). The threshold is **recalculated after every
> adjustment** from the new open legs — not fixed at entry.

**Action when triggered:**

1. Identify the **profitable leg** — the one whose current LTP is lower (seller's gain)
2. **Close** the profitable leg (buy it back)
3. **Find a new strike** to re-sell on the same side:
   - Target LTP ≈ the losing leg's current LTP (makes both premiums close to equal)
   - Must be **OTM only** — CE strike ≥ ATM, PE strike ≤ ATM (never ITM)
   - Can go up to ATM but not past it
   - Strike boundary: CE can only move inward toward ATM (never past original PE strike), PE can only move inward toward ATM (never past original CE strike)
4. **Sell** the new strike

**Straddle guard:**
- Once CE strike == PE strike (straddle formed), `adj_straddle = True`
- No further adjustments after this point

---

## Exit Rules

| Condition | Formula | Notes |
|---|---|---|
| Profit target | `pnl >= total_premium_collected × 0.70` | 70% of credit collected |
| Stop loss | `pnl <= -(total_premium_collected × STOP_LOSS_MULTIPLIER)` | Loss exceeds 1.5× credit |
| Expiry close | Friday 15:16 IST | Force-close before weekly expiry |

---

## State Tracked on Trade Object

| Attribute | Description |
|---|---|
| `adj_entry_premium` | Current ₹ threshold (recalculated after each adjustment) |
| `adj_count` | Number of adjustments completed |
| `adj_ce_strike_low` | Floor for CE rolls (= original PE strike) |
| `adj_pe_strike_high` | Ceiling for PE rolls (= original CE strike) |
| `adj_straddle` | `True` once straddle is formed — no more adjustments |