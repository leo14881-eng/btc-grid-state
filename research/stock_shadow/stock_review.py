#!/usr/bin/env python3
"""Winner/loser cohort review from the persistent Stock Shadow ledger."""
import json
from pathlib import Path
R=Path("research/results/stock-shadow"); P=R/"portfolio-v1.json"; O=R/"review-v1.json"
def main():
    d=json.loads(P.read_text()) if P.exists() else {"closed":[]}
    c=d.get("closed",[]); w=[x for x in c if x.get("realized_net_pnl_usdt",0)>0]; l=[x for x in c if x.get("realized_net_pnl_usdt",0)<=0]
    def stats(xs):
        if not xs:return {"n":0}
        return {"n":len(xs),"avg_net_pnl_usdt":sum(x.get("realized_net_pnl_usdt",0) for x in xs)/len(xs),"avg_mfe_net_pct":sum(x.get("mfe_net_pct",0) for x in xs)/len(xs),"avg_mae_net_pct":sum(x.get("mae_net_pct",0) for x in xs)/len(xs),"avg_tranches":sum(len(x.get("tranches",[])) for x in xs)/len(xs)}
    o={"winner_cohort":stats(w),"loser_cohort":stats(l),"note":"Forward cohort statistics only; insufficient samples remain insufficient rather than fabricated."}
    O.parent.mkdir(parents=True,exist_ok=True); O.write_text(json.dumps(o,indent=2)+"\n"); print(json.dumps(o))
if __name__=="__main__": main()
