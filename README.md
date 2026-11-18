# market-alerts

A small daily check that emails when market conditions are first met: VIX thresholds, index levels, S&P 500 trend and breadth signals (200-day average, death cross, share of stocks above their 200-day, a 20% fall), and IBD-style follow-through days on the Nasdaq Composite and S&P 500.

Runs on GitHub Actions after each US close. Levels live in the `ALERT_CONFIG` secret, not in the code. No broker connection and no orders. Not investment advice.
