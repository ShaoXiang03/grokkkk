# Strategy books 2026-11-10

## What was tested

E1–E6 from the research note, as portfolio and trend books on Binance USDT-M perpetuals. Development is 2020-01-01 through 2024-12-31. Each family has a pre-declared primary and at most two alternatives. Walk-forward uses the existing 182-day in-sample and 91-day out-of-sample windows, restarted flat at each window so a lucky in-sample equity does not size the next window. A non-primary variant is frozen only if it wins a strict majority of in-sample fold votes and its pooled walk-forward out-of-sample return and Sharpe are both positive. The 2025-01-01 through 2026-10-31 hold-out is run once on that frozen variant. Starting equity is $10,000. Costs are a 0.05% taker fee per fill, 4 bps slippage on the top 10 names by trailing 30-day quote volume and 8 bps otherwise, and the archived funding prints. v0.3 baseline, from the merged full-universe study, is -0.076R over 10,512 trades. It was not re-simulated here.

## Open-risk translation

Weekly books size to a 1.5× ATR(14) shock, not to the 5× ATR catastrophe stop. The 5× rule is a close-only disaster brake. Using it as the risk denominator would shrink the book by another 5/1.5 and would leave the 6% budget idle. A gap through the 1.5× shock can lose more than the budgeted risk before the brake fills. Each new name requests 2% of equity (1% on the E6 fade). A cluster above 3% is scaled down first (BTC, ETH, a fixed L1 list, and the rest are separate; longs and shorts in one cluster both count). The book is then scaled so total open risk is at most 6%. Notional is clipped at 3× equity and the freed risk is not reallocated. An all-alt book therefore runs at 3% open risk, not 6%, split across the names. A slice below 0.05% of equity is dropped; the scanner's 0.5% single-trade floor is not applied, because it would delete every name in a 12-name alt cluster. E2 uses one gross figure per leg so the legs match before the 3× clip. E3 and E6 risk to the stop they actually exit on. E5 targets 20% annualised volatility and is then scaled down until the same caps hold. It is not scaled up to the caps.

## Universe

At each decision close, a name must already have a daily bar, 60 closes (200 for E5 and E6-B), trailing 30-day quote volume of at least $20m per day, and, when an open-interest print exists within 3 days, at least $5m of OI. Missing OI does not exclude a name. Stables and a fixed list of tokenized equities and index perps are removed before the top-N rank. Delisted contracts stay eligible on days they still print and drop out the day they do not. The rank and the signal use only that close and earlier bars. Orders fill at the next day's open.

## Headline

| Book | Frozen | Dev CAGR | Dev Sharpe | Dev max DD | WF OOS Sharpe | Hold CAGR | Hold Sharpe | Hold max DD | Hold return | Enabled |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| E1 | e1_lb21_equal | 17.8% | 1.05 | 21.3% | 1.12 | -8.4% | -0.36 | 23.4% | -14.9% | no |
| E2 | e2_r15_b60_v30_top25 | 12.8% | 1.00 | 14.2% | 1.00 | 14.3% | 1.10 | 7.5% | 27.8% | yes |
| E3 | e3a_sma50_trail3_top40 | 34.2% | 1.68 | 12.8% | 1.46 | 16.9% | 0.97 | 12.3% | 33.1% | yes |
| E4 | e4_premium_7d | 18.3% | 1.18 | 21.3% | 1.17 | -25.2% | -1.64 | 41.7% | -41.2% | no |
| E5 | e5_daily_trail25 | 2.7% | 0.28 | 24.2% | 0.26 | -9.5% | -1.19 | 20.0% | -16.7% | no |
| E6 | e6b_crowded_squeeze | 3.2% | 0.68 | 4.5% | 0.82 | 2.3% | 0.41 | 8.7% | 4.3% | yes |

BTC 1x price path, entry and exit costs, no funding: development CAGR 66.9%, Sharpe 1.12, max DD 76.7%. Hold-out CAGR -10.8%, Sharpe -0.05, max DD 53.0%. A funded 1x perp long is a different trade: it pays the archived funding rate on the mark notional, so the cash drag stays after the price gives the gains back.

Long BTC / short equal-weight liquid alts, 50/50 notional, weekly, costs on, risk cap off: development CAGR 0.2%, Sharpe 0.15, max DD 62.7%. Hold-out CAGR -3.2%, Sharpe 0.03, max DD 41.8%.

The survivor book is an equal weight of the hold-out daily returns. It was assembled after seeing which sleeves were positive, so it is not itself an out-of-sample test.
Hold-out Sharpe against the equal-weight survivor book: E2 Sharpe 1.10 vs combo 1.34; E3 Sharpe 0.97 vs combo 1.34; E6 Sharpe 0.41 vs combo 1.34.
Survivor book CAGR 11.6%, Sharpe 1.34, max DD 4.7%. That Sharpe is higher, and the drawdown smaller, than each sleeve alone. The CAGR is not: it sits below the stronger sleeves because the book equal-weights a thin event sleeve with the two portfolio sleeves.

## Per book

### E1

Frozen `e1_lb21_equal`. no strict in-sample majority; primary kept. Votes: {'e1_lb21_equal': 5, 'e1_lb30_equal': 8, 'e1_lb21_invvol': 4}.

Development: CAGR 17.8%, Sharpe 1.05, max DD 21.3%, turnover 28.07×/year, fees $1,027, slippage $1,333, funding PnL $2,117, days in market 88.2%, average names 13.4, average gross leverage 0.41, correlation to BTC 0.05, trades 1773, expectancy R 0.03.

Walk-forward OOS (stitched, each window restarted): Sharpe 1.12, CAGR 20.1%, max DD 21.3%, total return 125.1%.

Hold-out: CAGR -8.4%, Sharpe -0.36, max DD 23.4%, total return -14.9%, turnover 30.71×/year, fees $278, slippage $377, funding PnL $1,570, correlation to BTC -0.06, trades 785, expectancy R 0.04, days in market 98.8%.

Trials in this family: 3. Deflated Sharpe on the development path of the frozen variant: 0.975. Deflated Sharpe on the hold-out path, using the same trial count: 0.241. Scott Phillips 49-day regression × R², and the N=25/60 universe, were not estimated.

| Variant | Dev CAGR | Dev Sharpe | Dev max DD | WF OOS Sharpe | WF OOS return |
| --- | --- | --- | --- | --- | --- |
| e1_lb21_equal (frozen) | 17.8% | 1.05 | 21.3% | 1.12 | 125.1% |
| e1_lb30_equal | 13.8% | 0.91 | 18.5% | 0.97 | 89.0% |
| e1_lb21_invvol | 7.1% | 0.68 | 11.6% | 0.69 | 37.7% |

### E2

Frozen `e2_r15_b60_v30_top25`. no strict in-sample majority; primary kept. Votes: {'e2_r21_b60_v30_top25': 7, 'e2_r15_b60_v30_top25': 8, 'e2_r10_b60_v30_top25': 2}.

Development: CAGR 12.8%, Sharpe 1.00, max DD 14.2%, turnover 22.25×/year, fees $805, slippage $1,062, funding PnL $1,837, days in market 83.3%, average names 6.6, average gross leverage 0.30, correlation to BTC 0.02, trades 1096, expectancy R 0.03.

Walk-forward OOS (stitched, each window restarted): Sharpe 1.00, CAGR 13.0%, max DD 14.4%, total return 72.3%.

Hold-out: CAGR 14.3%, Sharpe 1.10, max DD 7.5%, total return 27.8%, turnover 20.71×/year, fees $225, slippage $298, funding PnL $1,302, correlation to BTC -0.02, trades 470, expectancy R 0.06, days in market 98.8%.

Trials in this family: 3. Deflated Sharpe on the development path of the frozen variant: 0.962. Deflated Sharpe on the hold-out path, using the same trial count: 0.891. Beta 45/90 and vol 20 were not estimated. Residual horizon is the only searched axis. Top-40 was not crossed in.

| Variant | Dev CAGR | Dev Sharpe | Dev max DD | WF OOS Sharpe | WF OOS return |
| --- | --- | --- | --- | --- | --- |
| e2_r15_b60_v30_top25 (frozen) | 12.8% | 1.00 | 14.2% | 1.00 | 72.3% |
| e2_r10_b60_v30_top25 | 9.9% | 0.75 | 22.1% | 0.68 | 46.8% |
| e2_r21_b60_v30_top25 | 17.8% | 1.27 | 14.1% | 1.42 | 133.4% |

### E3

Frozen `e3a_sma50_trail3_top40`. no strict in-sample majority; primary kept. Votes: {'e3a_sma50_trail3_top40': 9, 'e3b_donchian20_trail3_top40': 7, 'e3a_sma50_trail3_majors': 2}.

Development: CAGR 34.2%, Sharpe 1.68, max DD 12.8%, turnover 15.32×/year, fees $988, slippage $1,373, funding PnL $-2,163, days in market 96.7%, average names 21.1, average gross leverage 0.29, correlation to BTC 0.11, trades 1893, expectancy R 0.14.

Walk-forward OOS (stitched, each window restarted): Sharpe 1.46, CAGR 30.7%, max DD 17.8%, total return 227.9%.

Hold-out: CAGR 16.9%, Sharpe 0.97, max DD 12.3%, total return 33.1%, turnover 18.55×/year, fees $181, slippage $252, funding PnL $1,829, correlation to BTC -0.16, trades 748, expectancy R 0.12, days in market 99.3%.

Trials in this family: 3. Deflated Sharpe on the development path of the frozen variant: 0.999. Deflated Sharpe on the hold-out path, using the same trial count: 0.828. Trail 2× and 4×, the BTC 50-day gate, and the 55-day Donchian were not estimated. The exit multiple is frozen at 3.

| Variant | Dev CAGR | Dev Sharpe | Dev max DD | WF OOS Sharpe | WF OOS return |
| --- | --- | --- | --- | --- | --- |
| e3a_sma50_trail3_top40 (frozen) | 34.2% | 1.68 | 12.8% | 1.46 | 227.9% |
| e3a_sma50_trail3_majors | 20.2% | 1.09 | 21.0% | 0.97 | 109.3% |
| e3b_donchian20_trail3_top40 | 18.4% | 1.11 | 18.8% | 1.50 | 223.2% |

### E4

Frozen `e4_premium_7d`. non-primary won a strict majority and its pooled walk-forward OOS was positive. Votes: {'e4_premium_7d': 12, 'e4_funding_7d': 5}.

Development: CAGR 18.3%, Sharpe 1.18, max DD 21.3%, turnover 34.18×/year, fees $1,429, slippage $1,866, funding PnL $3,236, days in market 88.2%, average names 13.5, average gross leverage 0.45, correlation to BTC 0.13, trades 2246, expectancy R 0.03.

Walk-forward OOS (stitched, each window restarted): Sharpe 1.17, CAGR 18.6%, max DD 26.6%, total return 112.9%.

Hold-out: CAGR -25.2%, Sharpe -1.64, max DD 41.7%, total return -41.2%, turnover 35.78×/year, fees $246, slippage $327, funding PnL $1,305, correlation to BTC -0.03, trades 844, expectancy R -0.03, days in market 98.8%.

Trials in this family: 3. Deflated Sharpe on the development path of the frozen variant: 0.947. Deflated Sharpe on the hold-out path, using the same trial count: 0.002. The daily-rebalance cost stress was not estimated. The headline is the weekly book.

| Variant | Dev CAGR | Dev Sharpe | Dev max DD | WF OOS Sharpe | WF OOS return |
| --- | --- | --- | --- | --- | --- |
| e4_funding_7d | 6.8% | 0.48 | 28.1% | 0.55 | 42.1% |
| e4_premium_7d (frozen) | 18.3% | 1.18 | 21.3% | 1.17 | 112.9% |
| e4_funding_z | 0.4% | 0.10 | 14.5% | 0.13 | 3.2% |

### E5

Frozen `e5_daily_trail25`. no strict in-sample majority; primary kept. Votes: {'e5_daily_trail15': 5, 'e5_daily_no_oi': 7, 'e5_daily_trail25': 4}.

Development: CAGR 2.7%, Sharpe 0.28, max DD 24.2%, turnover 30.98×/year, fees $874, slippage $1,207, funding PnL $-1,775, days in market 85.8%, average names 6.6, average gross leverage 0.18, correlation to BTC -0.02, trades 2259, expectancy R 0.02.

Walk-forward OOS (stitched, each window restarted): Sharpe 0.26, CAGR 2.6%, max DD 25.1%, total return 12.1%.

Hold-out: CAGR -9.5%, Sharpe -1.19, max DD 20.0%, total return -16.7%, turnover 34.59×/year, fees $289, slippage $418, funding PnL $-218, correlation to BTC -0.12, trades 1380, expectancy R -0.03, days in market 99.3%.

Trials in this family: 3. Deflated Sharpe on the development path of the frozen variant: 0.662. Deflated Sharpe on the hold-out path, using the same trial count: 0.039. The 30% and 40% vol targets were not estimated. The book is targeted at 20% and then scaled down to the caps. No BTC hedge overlay.

| Variant | Dev CAGR | Dev Sharpe | Dev max DD | WF OOS Sharpe | WF OOS return |
| --- | --- | --- | --- | --- | --- |
| e5_daily_trail25 (frozen) | 2.7% | 0.28 | 24.2% | 0.26 | 12.1% |
| e5_daily_trail15 | 2.5% | 0.23 | 33.3% | 0.21 | 9.5% |
| e5_daily_no_oi | 4.9% | 0.45 | 24.5% | 0.47 | 26.5% |

### E6

Frozen `e6b_crowded_squeeze`. non-primary won a strict majority and its pooled walk-forward OOS was positive. Votes: {'e6b_crowded_squeeze': 3}.

Development: CAGR 3.2%, Sharpe 0.68, max DD 4.5%, turnover 0.80×/year, fees $21, slippage $29, funding PnL $790, days in market 8.0%, average names 0.1, average gross leverage 0.01, correlation to BTC 0.05, trades 18, expectancy R 0.47.

Walk-forward OOS (stitched, each window restarted): Sharpe 0.82, CAGR 4.1%, max DD 3.8%, total return 19.3%.

Hold-out: CAGR 2.3%, Sharpe 0.41, max DD 8.7%, total return 4.3%, turnover 1.96×/year, fees $19, slippage $27, funding PnL $99, correlation to BTC 0.04, trades 11, expectancy R 0.21, days in market 15.2%.

Trials in this family: 2. Deflated Sharpe on the development path of the frozen variant: 0.602. Deflated Sharpe on the hold-out path, using the same trial count: 0.409. No third knob. The two variants are the pre-registered E6-A and E6-B rules.

| Variant | Dev CAGR | Dev Sharpe | Dev max DD | WF OOS Sharpe | WF OOS return |
| --- | --- | --- | --- | --- | --- |
| e6a_oi_flush | -4.7% | -0.89 | 25.6% | -0.96 | -21.9% |
| e6b_crowded_squeeze (frozen) | 3.2% | 0.68 | 4.5% | 0.82 | 19.3% |

## Correlation

Pearson correlation of daily simple returns. Development window, frozen variant.

|  | E1 | E2 | E3 | E4 | E5 | E6 | BTC |
| --- | --- | --- | --- | --- | --- | --- | --- |
| E1 | 1.00 | 0.42 | 0.05 | 0.05 | -0.10 | 0.05 | 0.05 |
| E2 | 0.42 | 1.00 | 0.21 | 0.12 | 0.09 | 0.09 | 0.02 |
| E3 | 0.05 | 0.21 | 1.00 | 0.15 | 0.52 | 0.09 | 0.11 |
| E4 | 0.05 | 0.12 | 0.15 | 1.00 | 0.06 | 0.05 | 0.13 |
| E5 | -0.10 | 0.09 | 0.52 | 0.06 | 1.00 | -0.01 | -0.02 |
| E6 | 0.05 | 0.09 | 0.09 | 0.05 | -0.01 | 1.00 | 0.05 |

## What is enabled

Enabled only where the hold-out terminal return is positive after costs: `e2_residual_momentum`, `e3_slow_trend`, `e6_liquidation_proxy`. The others stay disabled. A positive hold-out on one window is not a licence to trade size.
E6 clears that rule on a handful of hold-out trades and a low deflated Sharpe. The flag is the mechanical rule. It is not evidence of a liquidation-fade edge.

## Caveats

- The 6% cap is open risk to a 1.5× ATR shock for the weekly books, and to the real stop for E3, E5, and E6. It is not a 6% stop on equity. A gap can lose more.
- Delisted versus still listed is inferred from the last Vision daily bar. A name that died in the last three days of the archive can be labelled TRADING. The backtest itself uses whether a bar exists on the decision day.
- Metrics start in September 2020. Before that, the OI floor is not applied. The archive pull keeps the last daily snapshot for the point-in-time top 80 names by 30-day quote volume, plus BTC and ETH. That covers the top-40 book and a buffer for names the $5m OI floor can drop. E5 and E6 sit flat until funding z-scores and OI exist.
- Funding notional uses that day's daily close as the mark for the 08:00 and 16:00 prints. The rate is the archived settlement. The mark is not a signal.
- Slippage is a fixed 4/8 bps, not a historical book. The 2/4 bp sensitivity was not run, so the cost number is the harsher of the two schedules in the note.
- The v0.3 −0.076R figure is the merged study. This run did not reload the 1h and 4h archive.
- 17 variants were estimated across six families. Deflated Sharpe is per family, using that family's trial count, not a joint haircut across every book.
- Restarted walk-forward windows begin flat. A trend position that would have been open is re-entered only when the rule says so again.

