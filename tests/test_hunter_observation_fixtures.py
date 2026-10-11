"""Explicit synthetic coverage for tests that do not exercise acquisition."""
import datetime as dt
from research import hunter_observation_history as history
from research.hunter_policy import C


def certify_horizons(pos, hours=None, default_return=3.):
    closed=dt.datetime.fromisoformat(pos.setdefault('closed_at_utc','2026-10-04T00:00:00+00:00'))
    for h in hours or C['REVIEW_HOURS']:
        end=closed+dt.timedelta(hours=h)
        row=pos.setdefault('post_exit_observation',{}).setdefault(f'{int(h)}h',{'max_return_from_sell_pct':default_return})
        row['coverage']=history.coverage({'next_open_ms':history.bounds(closed,end)[1]},closed,end)
    return pos


def bars_between(start,end,high=105.):
    first,stop=history.bounds(start,end)
    return [(t,t+history.STEP_MS-1,high) for t in range(first,stop,history.STEP_MS)]
