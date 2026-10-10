import copy
import datetime as dt
import json
import sys
import unittest

from scripts import sentinel_analysis_candidate as c


class AnalysisCandidateTests(unittest.TestCase):
    def setUp(self):
        self.now = dt.datetime(2026, 10, 6, 20, 45, tzinfo=c.UTC)
        self.evidence = {key: {'asof': c.stamp(self.now), 'source': 'fixture-public-source'}
                         for key in ('btc_spot', 'btc_structure', 'btc_oi', 'btc_funding')}
        self.payload = c.snapshot('Leading Warning Engine frozen test policy', self.evidence,
                                  {}, 'a' * 40, self.now)
        self.proposal = {'input_id': self.payload['input_id'], 'primary_state': 'EARLY_DOWNSIDE_BUILDING',
            'main_path': 'Fixture downside path, not a real market conclusion.',
            'leading_evidence': [{'domain': 'price_response', 'source_refs': ['btc_structure'],
                'observation': 'Fixture structure observation'},
                {'domain': 'leverage', 'source_refs': ['btc_oi'], 'observation': 'Fixture leverage observation'}],
            'reversal_triggers': ['Fixture fresh structure and leverage improvement'],
            'early_action': 'STOP_ADDING', 'confirmation_status': 'DIAGNOSTIC_ONLY', 'data_gaps': [],
            'domains': {d: {'direction': 'UNKNOWN', 'source_refs': [], 'note': 'Unknown in fixture'}
                        for d in c.DOMAINS}}
        self.proposal['domains']['price_response'] = {'direction': 'DOWN',
            'source_refs': ['btc_structure', 'btc_spot'], 'note': 'Fixture'}
        self.proposal['domains']['leverage'] = {'direction': 'DOWN',
            'source_refs': ['btc_oi', 'btc_funding'], 'note': 'Fixture'}

    def validate(self):
        return c.validate_proposal(self.payload, self.proposal, self.now)

    def test_valid_proposal_never_mutates_input_or_authorizes_capital(self):
        before = copy.deepcopy((self.payload, self.proposal))
        result = self.validate()
        self.assertEqual(before, (self.payload, self.proposal))
        self.assertFalse(result['canonical_write_allowed'])
        self.assertFalse(result['candidate_enabled'])
        self.assertFalse(result['real_trading_enabled'])
        self.assertFalse(result['new_capital_action_allowed'])
        self.assertIn('UNKNOWN_DOMAIN:macro', result['data_gaps'])

    def test_input_tamper_rejected(self):
        self.payload['evidence']['btc_oi']['open_interest'] = 123
        with self.assertRaisesRegex(ValueError, 'INPUT_HASH_MISMATCH'): self.validate()

    def test_old_response_cannot_bind_new_input(self):
        self.proposal['input_id'] = 'different'
        with self.assertRaisesRegex(ValueError, 'BINDING'): self.validate()

    def test_stale_and_future_input_rejected(self):
        for seconds in (-1, 601):
            with self.assertRaisesRegex(ValueError, 'INPUT_STALE'):
                c.validate_proposal(self.payload, self.proposal, self.now + dt.timedelta(seconds=seconds))

    def test_no_writer_real_trading_or_portfolio_fields_allowed(self):
        for field in ('writer', 'real_trading_enabled', 'portfolio', 'persist_verified'):
            other = dict(self.proposal, **{field: True})
            with self.assertRaisesRegex(ValueError, 'FIELDS_INVALID'):
                c.validate_proposal(self.payload, other, self.now)

    def test_price_intervals_cannot_count_as_two_independent_domains(self):
        self.proposal['leading_evidence'] = [
            {'domain': 'price_response', 'source_refs': [ref], 'observation': 'Fixture'}
            for ref in ('btc_spot', 'btc_structure')]
        with self.assertRaisesRegex(ValueError, 'INDEPENDENT'): self.validate()

    def test_oi_and_funding_cannot_count_as_two_independent_domains(self):
        self.proposal['leading_evidence'] = [
            {'domain': 'leverage', 'source_refs': [ref], 'observation': 'Fixture'}
            for ref in ('btc_oi', 'btc_funding')]
        with self.assertRaisesRegex(ValueError, 'INDEPENDENT'): self.validate()

    def test_model_cannot_relabel_oi_as_spot_demand(self):
        self.proposal['domains']['spot_demand'] = {'direction': 'DOWN',
            'source_refs': ['btc_oi'], 'note': 'False independence'}
        with self.assertRaisesRegex(ValueError, 'SOURCE_REF_UNVERIFIED'): self.validate()

    def test_missing_error_or_stale_sources_rejected(self):
        for record in ({'asof': c.stamp(self.now), 'error': 'bad schema'},
                       {'asof': c.stamp(self.now - dt.timedelta(hours=2))},
                       {'asof': '2026-10-06T20:45:00'}):
            payload = c.snapshot(self.payload['policy'], dict(self.evidence, btc_oi=record),
                                 {}, 'a' * 40, self.now)
            self.proposal['input_id'] = payload['input_id']
            with self.assertRaisesRegex(ValueError, 'SOURCE_REF_UNVERIFIED'):
                c.validate_proposal(payload, self.proposal, self.now)

    def test_all_domains_required_even_when_unknown(self):
        del self.proposal['domains']['macro']
        with self.assertRaisesRegex(ValueError, 'DOMAIN_REVIEW_INCOMPLETE'): self.validate()

    def test_confirmation_is_not_primary_state(self):
        self.proposal['primary_state'] = 'CANDIDATE_NOT_CONFIRMED'
        with self.assertRaisesRegex(ValueError, 'PRIMARY_STATE_INVALID'): self.validate()

    def test_detection_time_retained_only_for_same_primary_state(self):
        for state, expected in [('EARLY_DOWNSIDE_BUILDING', '2026-10-06T19:00:00Z'),
                                ('EARLY_UPSIDE_BUILDING', c.stamp(self.now))]:
            payload = c.snapshot(self.payload['policy'], self.evidence,
                {'primary_state': state, 'first_detected_at': '2026-10-06T19:00:00Z'}, 'a'*40, self.now)
            proposal = dict(self.proposal, input_id=payload['input_id'])
            self.assertEqual(c.validate_proposal(payload, proposal, self.now)['first_detected_at'], expected)

    def test_missing_command_never_attempts_credentials_or_model_call(self):
        with self.assertRaisesRegex(ValueError, 'AUTHORIZED_MODEL_COMMAND_REQUIRED'):
            c.run_model_command(self.payload, None)

    def test_failed_command_does_not_expose_stderr_secrets(self):
        argv = [sys.executable, '-c', 'import sys; print("secret-fixture",file=sys.stderr);sys.exit(1)']
        with self.assertRaisesRegex(ValueError, '^MODEL_COMMAND_FAILED$'):
            c.run_model_command(self.payload, argv)

    def test_model_command_uses_json_stdin_and_no_shell(self):
        result = c.run_model_command(self.payload, [sys.executable, '-c',
            'import json,sys; r=json.load(sys.stdin);print(json.dumps({"input_id":r["input"]["input_id"]}))'])
        self.assertEqual(result, {'input_id': self.payload['input_id']})

    def test_timeout_and_oversized_output_fail(self):
        with self.assertRaisesRegex(ValueError, 'MODEL_COMMAND_TIMEOUT'):
            c.run_model_command(self.payload, [sys.executable, '-c', 'import time;time.sleep(2)'], timeout=.05)
        with self.assertRaisesRegex(ValueError, 'MODEL_OUTPUT_TOO_LARGE'):
            c.run_model_command(self.payload, [sys.executable, '-c', 'print("x"*65537)'])


if __name__ == '__main__': unittest.main()
