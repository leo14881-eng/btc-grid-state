import hashlib
import json
import unittest
from scripts.hunter_deployment_status import deployment_view


class DeploymentVersionTests(unittest.TestCase):
    def setUp(self):
        self.raw = json.dumps({'current_generation_id': 'g',
            'last_successful_monitor_generation_id': 'g', 'state_revision': 'monitor-loaded'})
        self.health = {'status': 'SUCCESS', 'source': 'VULTR_SYSTEMD',
            'capital_authority': 'NONE_SHADOW_ONLY', 'real_trading_enabled': False,
            'main_readback_verified': True, 'main_readback_head_sha': 'readback',
            'completed_at_utc': '2026-10-07T00:00:00Z', 'source_head_sha': 'job-published'}
        self.server = {'head': {'returncode': 0, 'stdout': 'base-old\n'},
                       'cached_origin_main': {'returncode': 0, 'stdout': 'cache-old\n'}}

    def view(self, jobs, raw=None, ancestor=lambda a, b: True):
        return deployment_view(self.server, jobs, self.raw if raw is None else raw,
                               'pinned-main', ancestor, 'target-fix')

    def test_base_cache_and_job_source_are_distinct(self):
        r = self.view({'research': self.health})
        self.assertEqual(r['base_checkout_sha'], 'base-old')
        self.assertEqual(r['cached_origin_main_sha'], 'cache-old')
        self.assertFalse(r['cached_origin_is_live_lookup'])
        self.assertEqual(r['last_research_source_sha'], 'job-published')
        self.assertEqual(r['authoritative_main_readback_sha'], 'readback')
        self.assertIn('START_HEAD_NOT_SEPARATELY_RECORDED', r['job_source_provenance']['research'])

    def test_monitor_source_requires_exact_health_binding(self):
        h = dict(self.health, monitor_generation_id='g',
                 scheduler_health_sha256=hashlib.sha256(self.raw.encode()).hexdigest())
        r = self.view({'monitor': h})
        self.assertEqual(r['last_monitor_source_sha'], 'monitor-loaded')
        self.assertTrue(r['contains_target_commit']['monitor'])
        r = self.view({'monitor': h}, self.raw + ' ')
        self.assertEqual(r['last_monitor_source_sha'], 'UNKNOWN')
        self.assertEqual(r['authoritative_main_readback_sha'], 'UNKNOWN')

    def test_missing_health_is_unknown_not_cached_head(self):
        r = self.view({})
        self.assertEqual(r['last_discovery_source_sha'], 'UNKNOWN')
        self.assertEqual(r['authoritative_main_readback_sha'], 'UNKNOWN')

    def test_unverified_or_unsafe_readback_cannot_be_authoritative(self):
        for patch in ({'status': 'FAILURE'}, {'main_readback_verified': False},
                      {'real_trading_enabled': True}, {'capital_authority': 'REAL'},
                      {'source': 'GITHUB_ACTIONS'}, {'completed_at_utc': 'invalid'}):
            with self.subTest(patch=patch):
                r = self.view({'research': dict(self.health, **patch)})
                self.assertEqual(r['authoritative_main_readback_sha'], 'UNKNOWN')

    def test_readback_must_belong_to_pinned_main(self):
        self.assertEqual(self.view({'research': self.health}, ancestor=lambda a, b: False)
                         ['authoritative_main_readback_sha'], 'UNKNOWN')

    def test_latest_verified_readback_selected_with_job_provenance(self):
        newer = dict(self.health, completed_at_utc='2026-10-07T00:05:00Z',
                     main_readback_head_sha='new-readback')
        r = self.view({'discovery': self.health, 'research': newer})
        self.assertEqual(r['authoritative_main_readback_sha'], 'new-readback')
        self.assertEqual(r['readback_provenance'], 'LATEST_VERIFIED_JOB_READBACK:research')


if __name__ == '__main__':
    unittest.main()
