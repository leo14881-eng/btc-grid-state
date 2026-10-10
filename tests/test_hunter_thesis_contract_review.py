"""Offline synthetic contracts only; never reconstructed historical BUY theses."""
import copy
import datetime as dt
import unittest

from research import hunter_thesis_contract_review as review

NOW=dt.datetime(2026,10,10,12,0,tzinfo=dt.timezone.utc)
IDENTITY=dict(asset='ENA',venue='BINANCE_SPOT',symbol='ENAUSDT',market_type='spot')


def seal(packet):
    packet['payload_sha256']=review.digest(packet['values'])
    packet['receipt_sha256']=review.digest({k:v for k,v in packet.items() if k!='receipt_sha256'})
    return packet


def operand(scope,field='relative',unit='pct'):
    value=dict(scope=scope,field=field,unit=unit)
    if scope!='policy':value['receipt']='asset'
    return value


def expression(op='ge',right=None):
    return dict(op=op,left=operand('current'),right=right or operand('policy','min_relative'))


def fixture():
    contract=dict(schema='hunter_thesis_contract_draft_v1',identity=IDENTITY.copy(),
        shadow_id='synthetic-position',entry_generation_id='entry-g',entry_at=(NOW-dt.timedelta(days=1)).isoformat(),
        entry_source_ref='synthetic://original-buy',policy_ref='synthetic://original-policy-version',
        original_policy=dict(min_relative=dict(value=2.0,unit='pct')),
        sources=dict(asset=dict(kind='DERIVED_RELATIVE_WITH_INPUT_RECEIPTS',identity=IDENTITY.copy(),role='POSITION',
            max_age_seconds=3600,closed_window_required=True,window_ms=900000)),
        aggregations={d:{p:'all' for p in review.PROPOSITIONS} for d in review.DIMENSIONS},
        predicates=[dict(id='entry-relative',version='synthetic-v1',dimension='relative_strength',
            proposition='entry_condition_still_holds',applicability='CONTINUING_THESIS',expression=expression()),
            dict(id='relative-deterioration',version='synthetic-v1',dimension='relative_strength',
            proposition='deteriorated_since_entry',applicability='CONTINUING_THESIS',
            expression=expression('lt',operand('entry')))])
    contract['original_policy_sha256']=review.digest(contract['original_policy'])
    packets={}
    for scope,clock,generation,value in [('entry',NOW-dt.timedelta(days=1),'entry-g',3.0),('current',NOW,'monitor-g',2.5)]:
        observed=clock-dt.timedelta(seconds=1)
        end=int(clock.timestamp()*1000)-1
        # Source window is fully closed before observation, not the live bar.
        end-=900000
        packets[scope]=dict(asset=seal(dict(kind=contract['sources']['asset']['kind'],identity=IDENTITY.copy(),
            source_ref='synthetic://'+scope,receipt_id=scope+'-receipt',generation_id=generation,
            source_observed_at=observed.isoformat(),fetched_at=clock.isoformat(),
            source_window=dict(start_ms=end-899999,end_ms=end,complete=True),
            values=dict(relative=dict(value=value,unit='pct')))))
    return contract,packets


class ThesisContractTests(unittest.TestCase):
    def run_contract(self,contract=None,packets=None,**changes):
        if contract is None:contract,packets=fixture()
        args=dict(identity=IDENTITY,now=NOW,generation='monitor-g',shadow_id='synthetic-position')
        args.update(changes)
        return review.evaluate(contract,packets,**args)

    def dimension(self,result,proposition='entry_condition_still_holds',dimension='relative_strength'):
        return result['dimensions'][dimension][proposition]

    def test_lower_than_entry_does_not_mean_original_condition_failed(self):
        result=self.run_contract()
        self.assertEqual(self.dimension(result)['value'],'TRUE')
        self.assertEqual(self.dimension(result,'deteriorated_since_entry')['value'],'TRUE')
        self.assertEqual(self.dimension(result,'hard_invalidation_confirmed')['value'],'UNKNOWN')
        self.assertFalse(result['original_thesis_coverage_complete'])
        self.assertFalse(result['hard_exit_authorized']);self.assertFalse(result['rebound_exit_authorized'])

    def test_missing_original_contract_cannot_be_replaced_with_current_values(self):
        result=self.run_contract({}, {})
        self.assertTrue(all(row['value']=='UNKNOWN' for d in result['dimensions'].values() for row in d.values()))
        self.assertFalse(result['original_thesis_authenticated'])

    def test_old_policy_is_used_and_unit_bound(self):
        contract,packets=fixture()
        packets['current']['asset']['values']['relative']['value']=1.9;seal(packets['current']['asset'])
        self.assertEqual(self.dimension(self.run_contract(contract,packets))['value'],'FALSE')
        contract['original_policy']['min_relative']['unit']='fraction'
        contract['original_policy_sha256']=review.digest(contract['original_policy'])
        self.assertEqual(self.dimension(self.run_contract(contract,packets))['value'],'UNKNOWN')

    def test_edited_policy_without_original_hash_is_unknown(self):
        contract,packets=fixture();contract['original_policy']['min_relative']['value']=0
        self.assertEqual(self.dimension(self.run_contract(contract,packets))['value'],'UNKNOWN')

    def test_shadow_asset_and_venue_identity_all_bind(self):
        for change in [dict(shadow_id='different-position'),dict(identity={**IDENTITY,'asset':'PENDLE'}),
                       dict(identity={**IDENTITY,'venue':'BYBIT_SPOT'})]:
            with self.subTest(change=change):
                self.assertEqual(self.dimension(self.run_contract(**change))['value'],'UNKNOWN')

    def test_wrong_generation_does_not_revalidate_current_thesis(self):
        contract,packets=fixture();packets['current']['asset']['generation_id']='previous';seal(packets['current']['asset'])
        self.assertEqual(self.dimension(self.run_contract(contract,packets))['value'],'UNKNOWN')

    def test_new_wrapper_cannot_refresh_old_source_or_closed_window(self):
        for field in ('source_observed_at','source_window'):
            with self.subTest(field=field):
                contract,packets=fixture();packet=packets['current']['asset']
                packet['verified_at']=NOW.isoformat()
                if field=='source_observed_at':packet[field]=(NOW-dt.timedelta(days=1)).isoformat()
                else:
                    for key in ('start_ms','end_ms'):packet[field][key]-=86400000
                seal(packet)
                self.assertEqual(self.dimension(self.run_contract(contract,packets))['value'],'UNKNOWN')

    def test_metadata_integrity_includes_source_timestamp_and_identity(self):
        contract,packets=fixture();packets['current']['asset']['source_observed_at']=NOW.isoformat()
        self.assertEqual(self.dimension(self.run_contract(contract,packets))['value'],'UNKNOWN')

    def test_future_naive_and_missing_source_clocks_are_unknown(self):
        for clock in [(NOW+dt.timedelta(seconds=1)).isoformat(),NOW.replace(tzinfo=None).isoformat(),None]:
            contract,packets=fixture();packets['current']['asset']['source_observed_at']=clock;seal(packets['current']['asset'])
            self.assertEqual(self.dimension(self.run_contract(contract,packets))['value'],'UNKNOWN')
        self.assertEqual(self.dimension(self.run_contract(now=NOW.replace(tzinfo=None)))['value'],'UNKNOWN')

    def test_partial_and_wrong_duration_windows_cannot_count_as_closed(self):
        for changes in [dict(complete=False),dict(complete='yes'),dict(end_ms=int(NOW.timestamp()*1000)+1),dict(start_ms=0)]:
            contract,packets=fixture();packets['current']['asset']['source_window'].update(changes);seal(packets['current']['asset'])
            self.assertEqual(self.dimension(self.run_contract(contract,packets))['value'],'UNKNOWN')

    def test_alignment_validates_benchmark_peer_itself(self):
        contract,packets=fixture();contract['sources']['asset']['alignment_group']='pair'
        contract['sources']['btc']=copy.deepcopy(contract['sources']['asset'])
        contract['sources']['btc']['identity']={**IDENTITY,'asset':'BTC','symbol':'BTCUSDT'}
        contract['sources']['btc'].update(role='BENCHMARK',benchmark_id='BTC')
        contract['benchmarks']=dict(BTC=dict(identity=contract['sources']['btc']['identity'].copy(),purpose='relative_strength_reference'))
        for scope in packets:
            packets[scope]['btc']=copy.deepcopy(packets[scope]['asset'])
            packets[scope]['btc']['identity']=contract['sources']['btc']['identity'].copy();seal(packets[scope]['btc'])
        self.assertEqual(self.dimension(self.run_contract(contract,packets))['value'],'TRUE')
        packets['current']['btc']['generation_id']='old';seal(packets['current']['btc'])
        self.assertEqual(self.dimension(self.run_contract(contract,packets))['value'],'UNKNOWN')
        packets['current']['btc']['generation_id']='monitor-g'
        for key in ('start_ms','end_ms'):packets['current']['btc']['source_window'][key]-=900000
        seal(packets['current']['btc'])
        self.assertEqual(self.dimension(self.run_contract(contract,packets))['value'],'UNKNOWN')

    def test_missing_demand_cannot_be_invented_from_total_volume(self):
        contract,packets=fixture();predicate=contract['predicates'][0]
        predicate['dimension']='volume_demand';predicate['expression']['left']['field']='taker_buy_ratio'
        packets['current']['asset']['values']['total_volume']=dict(value=1000000,unit='USDT');seal(packets['current']['asset'])
        self.assertEqual(self.dimension(self.run_contract(contract,packets),dimension='volume_demand')['value'],'UNKNOWN')

    def test_candidate_disappearance_is_unknown_not_false_or_hard(self):
        contract,packets=fixture();predicate=contract['predicates'][0]
        predicate['dimension']='candidate_evidence';predicate['expression']['left']['field']='current_candidate'
        result=self.run_contract(contract,packets)
        self.assertEqual(self.dimension(result,dimension='candidate_evidence')['value'],'UNKNOWN')
        self.assertEqual(self.dimension(result,'hard_invalidation_confirmed','candidate_evidence')['value'],'UNKNOWN')

    def test_entry_only_and_capital_conditions_excluded_from_holding(self):
        for applicability in ('ENTRY_ONLY','ALLOCATION_ONLY'):
            contract,packets=fixture();contract['predicates'][0]['applicability']=applicability
            result=self.run_contract(contract,packets)
            self.assertEqual(self.dimension(result)['value'],'UNKNOWN')
            self.assertIn('entry-relative',result['excluded_entry_or_allocation_only'])

    def test_ordinary_failure_cannot_be_relabelled_as_hard(self):
        contract,packets=fixture();contract['predicates'][0]['proposition']='hard_invalidation_confirmed'
        row=self.dimension(self.run_contract(contract,packets),'hard_invalidation_confirmed')
        self.assertEqual(row['value'],'UNKNOWN')
        self.assertIn('ORDINARY_WEAKNESS',row['predicates'][0]['reason'])

    def test_explicit_hard_or_is_not_implicitly_changed_to_and(self):
        contract,packets=fixture();a=contract['predicates'][0]
        a.update(proposition='hard_invalidation_confirmed',applicability='HARD_CONDITION')
        b=copy.deepcopy(a);b['id']='opposite';b['expression']['op']='lt';contract['predicates'].append(b)
        contract['aggregations']['relative_strength']['hard_invalidation_confirmed']='any'
        result=self.run_contract(contract,packets)
        self.assertEqual(self.dimension(result,'hard_invalidation_confirmed')['value'],'TRUE')
        self.assertFalse(result['hard_exit_authorized'])
        del contract['aggregations']['relative_strength']['hard_invalidation_confirmed']
        self.assertEqual(self.dimension(self.run_contract(contract,packets),'hard_invalidation_confirmed')['value'],'UNKNOWN')

    def test_logical_false_does_not_claim_complete_evidence_coverage(self):
        contract,packets=fixture();template=contract['predicates'][0];contract['predicates']=[]
        for dimension in review.DIMENSIONS:
            predicate=copy.deepcopy(template);predicate.update(id=dimension,dimension=dimension)
            absent=expression();absent['left']['field']='not_recorded'
            predicate['expression']=dict(op='all',args=[expression('lt'),absent])
            contract['predicates'].append(predicate)
        result=self.run_contract(contract,packets)
        self.assertEqual(result['all_recorded_entry_conditions_hold'],'FALSE')
        self.assertFalse(result['original_thesis_coverage_complete'])
        self.assertTrue(all(not d['entry_condition_still_holds']['evidence_complete'] for d in result['dimensions'].values()))

    def test_complete_synthetic_coverage_still_does_not_authenticate_sources(self):
        contract,packets=fixture();template=contract['predicates'][0];contract['predicates']=[]
        for dimension in review.DIMENSIONS:
            p=copy.deepcopy(template);p.update(id=dimension,dimension=dimension);contract['predicates'].append(p)
        result=self.run_contract(contract,packets)
        self.assertTrue(result['original_thesis_coverage_complete'])
        self.assertFalse(result['original_thesis_authenticated']);self.assertFalse(result['production_activated'])

    def test_unversioned_duplicate_or_malformed_predicates_are_unknown(self):
        for issue in ('version','duplicate','expression'):
            contract,packets=fixture()
            if issue=='version':contract['predicates'][0].pop('version')
            elif issue=='duplicate':contract['predicates'].append(copy.deepcopy(contract['predicates'][0]))
            else:contract['predicates'][0].pop('expression')
            self.assertEqual(self.dimension(self.run_contract(contract,packets))['value'],'UNKNOWN')

    def test_none_nan_bool_and_wrong_units_are_never_numeric_weakness(self):
        for value in (None,float('nan'),True,'2.5'):
            contract,packets=fixture();packet=packets['current']['asset'];packet['values']['relative']['value']=value
            if isinstance(value,float) and value!=value:
                # NaN cannot even be canonically sealed; kernel must still fail closed.
                self.assertRaises(ValueError,seal,packet)
            else:seal(packet)
            self.assertEqual(self.dimension(self.run_contract(contract,packets))['value'],'UNKNOWN')
        contract,packets=fixture();packets['current']['asset']['values']['relative']['unit']='bps';seal(packets['current']['asset'])
        self.assertEqual(self.dimension(self.run_contract(contract,packets))['value'],'UNKNOWN')

    def test_evaluation_is_pure_repeatable_and_cannot_mutate_ledger(self):
        contract,packets=fixture();before=copy.deepcopy((contract,packets))
        first=self.run_contract(contract,packets);second=self.run_contract(contract,packets)
        self.assertEqual(first,second);self.assertEqual((contract,packets),before)
        self.assertFalse(first['real_trading_enabled']);self.assertEqual(first['capital_authority'],'NONE_SHADOW_ONLY')

    def test_three_valued_logic(self):
        self.assertEqual(review.aggregate(['FALSE','UNKNOWN']),'FALSE')
        self.assertEqual(review.aggregate(['TRUE','UNKNOWN'],'any'),'TRUE')
        self.assertEqual(review.aggregate(['TRUE','UNKNOWN']),'UNKNOWN')
        self.assertEqual(review.aggregate([]),'UNKNOWN')

    def test_position_source_cannot_borrow_another_asset_identity(self):
        contract,packets=fixture()
        other={**IDENTITY,'asset':'PENDLE','symbol':'PENDLEUSDT'}
        contract['sources']['asset']['identity']=other
        packets['current']['asset']['identity']=other;seal(packets['current']['asset'])
        self.assertEqual(self.dimension(self.run_contract(contract,packets))['value'],'UNKNOWN')
        contract['sources']['asset']['role']='BENCHMARK'
        self.assertEqual(self.dimension(self.run_contract(contract,packets))['value'],'UNKNOWN')

    def test_null_source_and_future_entry_fail_closed(self):
        contract,packets=fixture();packets['current']['asset']=None
        self.assertEqual(self.dimension(self.run_contract(contract,packets))['value'],'UNKNOWN')
        contract,packets=fixture();contract['entry_at']=(NOW+dt.timedelta(days=1)).isoformat()
        self.assertEqual(self.dimension(self.run_contract(contract,packets))['value'],'UNKNOWN')
        result=review.evaluate(None,None,IDENTITY,NOW,'monitor-g','synthetic-position')
        self.assertEqual(self.dimension(result)['value'],'UNKNOWN')

    def test_source_publication_effective_and_wrapper_clocks_are_distinct(self):
        contract,packets=fixture();spec=contract['sources']['asset'];packet=packets['current']['asset']
        spec.update(requires_publication_time=True,effective_time_rule='ALREADY_EFFECTIVE')
        packet.update(verified_at=NOW.isoformat(),source_published_at=(NOW-dt.timedelta(days=3)).isoformat(),
            effective_from=(NOW+dt.timedelta(days=1)).isoformat());seal(packet)
        self.assertEqual(self.dimension(self.run_contract(contract,packets))['value'],'UNKNOWN')
        spec['effective_time_rule']='SCHEDULED_FACT'
        self.assertEqual(self.dimension(self.run_contract(contract,packets))['value'],'TRUE')
        packet['source_published_at']=(NOW+dt.timedelta(seconds=1)).isoformat();seal(packet)
        self.assertEqual(self.dimension(self.run_contract(contract,packets))['value'],'UNKNOWN')

    def test_source_and_window_age_cannot_stack_two_ttls(self):
        contract,packets=fixture();packet=packets['current']['asset']
        observed=NOW-dt.timedelta(seconds=3599);end=int((observed-dt.timedelta(seconds=3599)).timestamp()*1000)
        packet.update(source_observed_at=observed.isoformat(),source_window=dict(start_ms=end-899999,end_ms=end,complete=True));seal(packet)
        self.assertEqual(self.dimension(self.run_contract(contract,packets))['value'],'UNKNOWN')

    def test_null_aggregation_is_unknown_not_an_exception(self):
        for aggregation in (None,dict(relative_strength=None)):
            contract,packets=fixture();contract['aggregations']=aggregation
            row=self.dimension(self.run_contract(contract,packets))
            self.assertEqual(row['value'],'UNKNOWN');self.assertFalse(row['evidence_complete'])


class ConfirmationTests(unittest.TestCase):
    def counter(self,rows):
        return review.consecutive_confirmations(rows,3,900000,'position:predicate:version')

    def row(self,index,value='TRUE'):
        return dict(series_key='position:predicate:version',source_closed_at_ms=index*900000,value=value)

    def test_no_observations_is_unknown(self):
        self.assertEqual(self.counter([])['value'],'UNKNOWN')

    def test_wrappers_do_not_create_consecutive_observations(self):
        rows=[{**self.row(1),'generation_id':'wrapper'+str(i)} for i in range(3)]
        result=self.counter(rows)
        self.assertEqual(result['count'],1);self.assertEqual(result['ignored_duplicate_or_old_windows'],2)

    def test_gap_unknown_and_recovery_reset_counter(self):
        for row in (self.row(4),self.row(3,'UNKNOWN'),self.row(3,'FALSE')):
            result=self.counter([self.row(1),self.row(2),row])
            self.assertLess(result['count'],3)
        self.assertEqual(self.counter([self.row(1),self.row(2),self.row(3)])['count'],3)

    def test_conflicting_old_or_same_window_invalidates_confirmation(self):
        for index in (1,3):
            result=self.counter([self.row(1),self.row(2),self.row(3),self.row(index,'FALSE')])
            self.assertEqual(result['value'],'UNKNOWN');self.assertEqual(result['count'],0)
            self.assertEqual(result['conflicting_same_window'],1)

    def test_different_positions_or_predicate_versions_cannot_share_counter(self):
        self.assertRaises(ValueError,self.counter,[self.row(1),{**self.row(2),'series_key':'another-position'}])
        self.assertRaises(ValueError,review.consecutive_confirmations,[],3,900000,'')

    def test_count_and_window_require_explicit_policy_and_never_trade(self):
        self.assertRaises(ValueError,review.consecutive_confirmations,[],True,900000,'series')
        self.assertRaises(ValueError,review.consecutive_confirmations,[],3,0,'series')
        self.assertFalse(self.counter([self.row(1),self.row(2),self.row(3)])['trade_authorized'])

    def test_old_conflict_outside_current_streak_is_audited_but_does_not_erase_it(self):
        result=self.counter([self.row(1),self.row(4),self.row(5),self.row(6),self.row(1,'FALSE')])
        self.assertEqual(result['count'],3);self.assertEqual(result['conflicting_same_window'],1)


if __name__=='__main__':unittest.main()
