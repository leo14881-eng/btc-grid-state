import unittest
from scripts.hunter_generation_admission import admit


class AdmissionTests(unittest.TestCase):
    scan = {"generation_id": "latest", "binance_complete": True,
            "source_head_sha": "vultr-current-main", "as_of_utc": "now"}

    def test_vultr_failed_research_backup_binds_main_despite_old_github_trigger(self):
        accepted, reason = admit(self.scan, {"scan_generation_id": "old"},
                                 "workflow_run", "old-github-trigger", primary=True)
        self.assertTrue(accepted)
        self.assertEqual(reason, "VULTR_PRIMARY_RESEARCH_FAILOVER")

    def test_unconfigured_stale_trigger_remains_rejected(self):
        self.assertFalse(admit(self.scan, {}, "workflow_run", "old-github-trigger")[0])
        self.assertFalse(admit(self.scan, {}, "workflow_run", None)[0])

    def test_aligned_generation_cannot_be_repeated_by_either_scheduler(self):
        for primary in (True, False):
            self.assertFalse(admit(self.scan, {"scan_generation_id": "latest"},
                                   "workflow_run", "vultr-current-main", primary=primary)[0])

    def test_manual_force_is_explicit_and_default_is_idempotent(self):
        health = {"scan_generation_id": "latest"}
        self.assertFalse(admit(self.scan, health, "workflow_dispatch", None)[0])
        self.assertTrue(admit(self.scan, health, "workflow_dispatch", None, force=True)[0])

    def test_matching_github_discovery_still_admitted(self):
        self.assertTrue(admit(self.scan, {}, "workflow_run", "vultr-current-main")[0])

    def test_invalid_discovery_fails_closed_even_for_force_and_primary(self):
        for scan in ({}, {**self.scan, "binance_complete": False}):
            with self.assertRaisesRegex(RuntimeError, "INCOMPLETE"):
                admit(scan, {}, "workflow_dispatch", None, force=True, primary=True)

    def test_unsupported_event_does_not_gain_primary_authority(self):
        self.assertFalse(admit(self.scan, {}, "push", None, primary=True)[0])
