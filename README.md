# ERCOT Virtual Battery

A simulated merchant battery trading operation in ERCOT, run as a public, verifiable forward track record.

Every afternoon, before the operating day begins, an automated job commits the next day's charge/discharge schedule for a simulated 100 MW / 200 MWh battery at ERCOT North Hub, optimized against published day-ahead prices. After the operating day, a second job settles the result, benchmarks it against the perfect-foresight real-time optimum, and appends one row to an append-only ledger. Git commit timestamps prove every schedule existed before its outcome was known.

## Integrity rules

- The ledger and committed schedules are append-only. Corrections are new flagged rows; originals stay intact.
- No backfilling. A missed commit is a SKIP row forever.
- No fabricated or interpolated price data.

## Status

Under construction. The pipeline is being built; no schedules have been committed yet.

## Disclaimer

This is independent research and a simulation. No real trading occurs, no real money is at risk, and nothing here is investment advice. All price data is public. This project is unaffiliated with any employer.
