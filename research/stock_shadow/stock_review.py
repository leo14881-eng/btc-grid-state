#!/usr/bin/env python3
"""Winner/loser cohort review from the persistent Stock Shadow ledger."""
import json
from datetime import datetime, timezone
from pathlib import Path
try:
    from .state_safety import number
except ImportError:
    from state_safety import number
R=Path("research/results/stock-shadow"); P=R/"portfolio-v1.json"; O=R/"review-v1.json"
def main():
    d=json.loads(P.read_text()) if P.exists() else {"closed":[]}
    c=d.get("closed",[]); w=[x for x in c if x.get("realized_net_pnl_usdt",0)>0]; l=[x for x in c if x.get("realized_net_pnl_usdt",0)<=0]
    def stats(xs):
        if not xs:return {"n":0}
        result={"n":len(xs),"avg_net_pnl_usdt":sum(number(x.get("realized_net_pnl_usdt"),"review.realized_net_pnl_usdt") for x in xs)/len(xs),"avg_tranches":sum(len(x.get("tranches",[])) for x in xs)/len(xs)}
        for field in ("mfe_net_pct","mae_net_pct"):
            values=[number(x[field],"review."+field) for x in xs if x.get(field) is not None]
            result["avg_"+field]=sum(values)/len(values) if values else None
            result[field+"_samples"]=len(values)
        return result
    o={"updated_at":datetime.now(timezone.utc).isoformat(),"portfolio_updated_at":d.get("updated_at"),"portfolio_run_id":d.get("run_id"),"portfolio_source_commit":d.get("source_commit"),"winner_cohort":stats(w),"loser_cohort":stats(l),"note":"Forward cohort statistics only; insufficient samples remain insufficient rather than fabricated."}
    O.parent.mkdir(parents=True,exist_ok=True); O.write_text(json.dumps(o,indent=2)+"\n"); print(json.dumps(o))
if __name__=="__main__": main()
