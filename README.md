# ERCOT Virtual Battery

A simulated grid battery that commits its trading schedule publicly, before the market clears, and scores itself afterward.

## Why a battery makes money

Electricity cannot be stored on the grid. It has to be produced the instant it is consumed, and price adjusts violently to force that balance. In Texas at midday, solar floods the system and prices sometimes go negative, meaning generators pay someone to take the power away. By evening the sun sets, air conditioners come on, and prices can jump by orders of magnitude within a few hours.

A grid battery moves electricity through time. It charges when power is cheap, discharges when power is expensive, and earns the spread. It generates nothing. It is a machine for buying low and selling high on a daily cycle, and the physical asset is rows of lithium containers on a concrete pad wired into a substation.

## Who does this in real life

Standalone merchant storage operators run this business with no generation attached: Jupiter Power, Key Capture Energy, Broad Reach Power, Plus Power. Integrated power companies such as Vistra and NextEra run storage alongside gas, coal, and nuclear fleets. Tesla mostly sells the picks and shovels, Megapack hardware plus Autobidder, the software that forecasts prices, co-optimizes across markets, and dispatches real batteries automatically. Commodity trading houses and hedge fund power desks trade the same volatility financially, without owning anything.

## Why it is not free money

The arbitrage is obvious, which is exactly why it is not a glitch. Four things stand between the spread and a profit.

**Capital.** A 100 MW / 200 MWh battery is a nine-figure asset that must repay itself over fifteen to twenty years while its cells degrade. The question was never whether arbitrage is profitable. It is whether arbitrage revenue exceeds the amortized cost of the steel and lithium bought to capture it.

**Crowding.** Batteries flatten the curve they feed on. Charging at midday lifts midday prices; discharging in the evening suppresses evening prices. Average ERCOT battery revenue fell from roughly $149/kW in 2023 toward a projected $17/kW in 2025 as capacity piled in. The glitch closed in public, in two years.

**Physics.** Ninety percent round-trip efficiency means the sale price must beat the purchase price by more than about eleven percent before the first dollar of profit exists. Cycling wears the cells, so every trade carries a real cost.

**Sequencing.** Arbitrage was never the good part. Most early ERCOT battery revenue came from ancillary services, being paid to stand by and respond within seconds when a generator trips. That market is small, it saturated first, and energy arbitrage is what operators fell back on.

An obvious arbitrage in a liquid market is not an inefficiency. It is the return on the capital and risk required to capture it. The spread is a price signal saying storage is needed here, and capital arrives until the spread compensates capital and no further.

## What this repository actually does

It simulates a 100 MW / 200 MWh battery with 90 percent round-trip efficiency, settling at an ERCOT hub, limited to one cycle per day.

Each afternoon, after ERCOT publishes day-ahead prices for the following operating day, the bot solves a linear program for the optimal charge and discharge schedule under the asset's physical constraints, then commits that schedule to this repository with a timestamp. The commit happens before the operating day begins. It is never edited afterward.

Once the operating day resolves, the bot settles the position, pulls realized real-time prices, computes what a clairvoyant operator could have earned with perfect foresight, and appends one immutable row to the public ledger.

The headline metric is capture rate: realized profit divided by the perfect-foresight optimum. Raw dollars flatter a strategy by rewarding volatile weeks. Capture rate asks the harder question. Given the spread that existed, how much of it did the strategy actually get.

A capex module carries the analysis one step further, testing whether the revenue the bot earns would ever justify building the asset, against installed cost, degradation, augmentation, and fixed operating costs. If simulated arbitrage revenue falls short of breakeven, that result is published as-is. It is the honest one, and it explains why operators chased ancillary services in the first place.

## Why the timestamps are the point

Anyone can backtest a battery strategy across historical prices and produce a flattering equity curve. Nobody should be impressed by one. This project publishes decisions before outcomes exist and never revises them, which makes the record forward-looking and impossible to fabricate after the fact. The append-only rule is not bookkeeping. It is the entire signal.

## Limitations, stated plainly

Simulation only. No capital is at risk and no bids are submitted to any market. The model assumes a price-taking self-schedule with no market impact, ignores bid-ask dynamics, excludes ancillary services and capacity revenue, and applies a simple degradation treatment. The asset does not exist. Any resemblance to the economics of a real merchant battery is approximate and deliberately conservative.

Independent research and simulation. Not investment advice. Unaffiliated with any employer.

---

Repository guide: [docs/project_context.pdf](docs/project_context.pdf) is the original context document this README is drawn from. Committed schedules live in `data/commits/`, one JSON per operating day, and settled results in `data/ledger.csv`, both append-only.
