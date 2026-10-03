# Stock Shadow

Independent paper-trading research lane for Bybit xStocks. It does not import, edit, or write Hunter V1/V2 files and never places real orders.

V1 deliberately casts a wide net: no portfolio-wide open-position cap, standardized paper tranches, full BUY/ADD event ledger, MFE/MAE tracking, and later winner/loser cohort analysis. Paid APIs are not required. Free API keys may be added later through GitHub Actions secrets.

All persistent outputs live under `research/results/stock-shadow/`.
