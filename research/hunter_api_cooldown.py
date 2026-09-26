#!/usr/bin/env python3
"""Shared persistent CoinGecko 429 cooldown across Hunter's independent stages.

The caller persists the state to research/results/ and must not bypass the
cooldown with retries. HTTP Retry-After is honored when available, bounded to
avoid accidental indefinite suspension. Does not change capital authority.
"""
import datetime as dt
import email.utils
import json
import pathlib

PATH=pathlib.Path("research/results/hunter-coingecko-throttle.json")
DEFAULT_SECONDS=3600
MAX_SECONDS=14400

def utc(value):
    if not isinstance(value,str):raise ValueError("timestamp required")
    t=dt.datetime.fromisoformat(value.replace("Z","+00:00"))
    if t.tzinfo is None:raise ValueError("timezone required")
    return t.astimezone(dt.timezone.utc)

def load(path=PATH):
    try:
        state=json.loads(path.read_text())
        if not isinstance(state,dict):return {}
        return state
    except (OSError,ValueError,TypeError):
        return {}

def blocked(state,now):
    try:
        return now.astimezone(dt.timezone.utc)<utc(state.get("blocked_until_utc"))
    except (ValueError,TypeError,AttributeError):
        return False

def record_429(state,now,retry_after=None,stage="unknown"):
    seconds=DEFAULT_SECONDS
    if retry_after is not None:
        try:
            seconds=float(retry_after)
        except (ValueError,TypeError):
            try:
                date=email.utils.parsedate_to_datetime(str(retry_after))
                seconds=(date-now).total_seconds()
            except (ValueError,TypeError,OverflowError):
                pass
    seconds=max(60,min(MAX_SECONDS,seconds))
    return {**(state or {}),"last_429_utc":now.isoformat(),
            "blocked_until_utc":(now+dt.timedelta(seconds=seconds)).isoformat(),
            "last_limited_stage":stage,"retry_delay_seconds":seconds}

def save(state,path=PATH):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(state,indent=2)+"\n")
