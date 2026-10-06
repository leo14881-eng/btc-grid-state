"""Admit current main research generations, including configured Vultr failover."""
import json
import os
import pathlib


def admit(scan, health, event, trigger_sha, force=False, primary=False):
    if not scan.get("generation_id") or scan.get("binance_complete") is not True:
        raise RuntimeError("INCOMPLETE_AUTHORITATIVE_DISCOVERY")
    aligned = (health.get("scan_generation_id") == scan["generation_id"] or
               health.get("scan_as_of_utc") == scan.get("as_of_utc")
               and bool(scan.get("as_of_utc")))
    if event == "workflow_dispatch":
        return (force or not aligned), "MANUAL_CURRENT_MAIN"
    if event != "workflow_run":
        return False, "UNSUPPORTED_TRIGGER"
    if aligned:
        return False, "GENERATION_ALREADY_ALIGNED"
    if primary:
        # The preceding runtime gate permits this bind only when current Vultr
        # research lacks verified success. Bind latest main, never a trigger's
        # stale checkout. Final generation guard and CAS remain mandatory.
        return True, "VULTR_PRIMARY_RESEARCH_FAILOVER"
    return bool(trigger_sha) and scan.get("source_head_sha") == trigger_sha, "MATCHED_DISCOVERY_TRIGGER"


def load(path):
    try:
        return json.loads(pathlib.Path(path).read_text())
    except (OSError, ValueError):
        return {}


def main():
    scan = load("research/results/hunter-cex-universe-run.json")
    accepted, reason = admit(
        scan, load("research/results/hunter-health-and-queue.json"),
        os.environ.get("TRIGGER_EVENT"), os.environ.get("TRIGGER_SHA"),
        os.environ.get("FORCE", "false").lower() == "true",
        load(".github/hunter-runtime.json").get("primary_jobs", {}).get("research") is True)
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        output.write("should_run=" + str(accepted).lower() + "\n")
        output.write("generation=" + scan["generation_id"] + "\n")
    print("HUNTER_GENERATION_ADMITTED" if accepted else "HUNTER_TRIGGER_SKIPPED",
          scan["generation_id"], reason)


if __name__ == "__main__":
    main()
