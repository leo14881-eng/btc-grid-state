"""Exercise real sanitization/page verification, including journalctl's null projection."""
import copy
import json
import subprocess
import unittest
from unittest.mock import patch

from scripts import hunter_binance_recover_three_once as recovery


def rows(total=1228,page_size=50):
    epoch='a'*32
    entries=[dict(event='BINANCE_SESSION_METRICS',epoch=epoch,final=True,topics=total,
                  received=total*10,processed=total*10,zero_received=0,zero_processed=0,audit_output_failures=0)]
    topics=[dict(topic='x'+str(i)+'usdt@kline_15m',received=10,processed=10,changed=10,
                 last_rx=1791848031000,last_processed_rx=1791848031000) for i in range(total)]
    for offset in range(0,total,page_size):
        entries.append(dict(event='BINANCE_TOPIC_METRICS',epoch=epoch,offset=offset,total=total,topics=topics[offset:offset+page_size]))
    return entries


class JournalTests(unittest.TestCase):
    def export(self,values,*,null_large=False,byte_arrays=False,require_final=False):
        def command(args,**kwargs):
            self.assertIn('--all',args);self.assertIn('--output-fields=MESSAGE',args)
            entries=[]
            for value in values:
                message=json.dumps(value) if isinstance(value,dict) else value
                if null_large and isinstance(message,str) and len(message.encode())>4096:message=None
                elif byte_arrays and isinstance(message,str):message=list(message.encode())
                entries.append(json.dumps(dict(MESSAGE=message,__REALTIME_TIMESTAMP='1791848031000000')))
            return subprocess.CompletedProcess(args,0,'\n'.join(entries).encode(),b'')
        with patch.object(recovery,'command',side_effect=command):return recovery.journal_evidence('fixture',require_final=require_final)

    def test_all_1228_topics_survive_old_50_topic_pages_above_4096_bytes(self):
        result=self.export(rows())
        self.assertTrue(result['complete']);self.assertEqual(result['topic_coverage'][0]['observed_topics'],1228)

    def test_old_null_projection_is_incomplete_instead_of_hash_of_none(self):
        result=self.export(rows(),null_large=True)
        self.assertFalse(result['complete']);self.assertIn('JOURNAL_MESSAGE_UNAVAILABLE',result['completeness_errors'])
        self.assertIn('TOPIC_METRICS_PAGES_INCOMPLETE',result['completeness_errors'])
        self.assertFalse(any(x.get('message_sha256')=='dc937b59892604f5a86ac96936cd7ff09e25f18ae6b758e8014a24c7fa039e91' for x in result['records']))

    def test_byte_array_fields_decode_without_exposing_unlisted_secrets(self):
        values=rows(33,16);values[1]['secret']='do-not-export';values[1]['topics'][0]['token']='do-not-export'
        result=self.export(values,byte_arrays=True)
        self.assertTrue(result['complete']);self.assertNotIn('do-not-export',json.dumps(result))

    def test_missing_duplicate_reordered_wrong_total_or_cross_epoch_pages_fail(self):
        original=rows(33,16)
        variants=[original[:-1],original+[original[-1]],original[:1]+original[2:]+original[1:2]]
        for key,value in [('offset',1),('total',34),('epoch','b'*32)]:
            changed=copy.deepcopy(original);changed[1][key]=value;variants.append(changed)
        for values in variants:
            with self.subTest(values=values[-1].get('offset')):self.assertFalse(self.export(values)['complete'])

    def test_duplicate_topics_invalid_counters_or_summary_disagreement_fail(self):
        for change in ['duplicate','counter','summary','null_item','bad_epoch']:
            values=rows(33,16)
            if change=='duplicate':values[1]['topics'][1]['topic']=values[1]['topics'][0]['topic']
            if change=='counter':values[1]['topics'][0]['processed']=11
            if change=='summary':values[0]['received']+=1
            if change=='null_item':values[1]['topics'][0]=None
            if change=='bad_epoch':values[1]['epoch']=[]
            with self.subTest(change=change):self.assertFalse(self.export(values)['complete'])

    def test_missing_null_malformed_or_ambiguous_messages_fail(self):
        for message in [None,'{incomplete',["two","fields"],[255],"a"*65537]:
            with self.subTest(message_type=type(message).__name__):
                self.assertFalse(self.export([message])['complete'])

    def test_active_epoch_requires_no_final_pages_but_record_limit_fails(self):
        active=dict(event='BINANCE_SESSION_METRICS',epoch='a'*32,final=False,topics=1228)
        self.assertTrue(self.export([active])['complete'])
        result=self.export(['plain journal text']*10000)
        self.assertFalse(result['complete']);self.assertTrue(result['at_limit'])

    def test_normal_startup_plan_json_is_sanitized_without_false_incompleteness(self):
        result=self.export([dict(topics=1228,sockets=3,rest_requests_per_attempt=1228,
                                 network_capacity_verified=False)])
        self.assertTrue(result['complete']);self.assertTrue(result['records'][0]['evidence']['raw_omitted'])

    def test_an_ended_epoch_cannot_lose_its_entire_final_set(self):
        active=dict(event='BINANCE_SESSION_METRICS',epoch='b'*32,final=False,topics=1228)
        for suffix in [[dict(event='BINANCE_STREAM_RECOVERY')]+rows(16),rows(16)]:
            self.assertFalse(self.export([active]+suffix)['complete'])
        self.assertFalse(self.export([active],require_final=True)['complete'])

    def test_raw_oversized_page_cannot_be_cleaned_into_valid_coverage(self):
        values=rows(50);values[1]['topics'].append(dict(values[1]['topics'][0],topic='extrausdt@kline_15m'))
        result=self.export(values)
        self.assertFalse(result['complete']);self.assertIn('TOPIC_METRICS_PAGE_SIZE_INVALID',result['completeness_errors'])

    def test_incomplete_journal_cannot_pass_existing_ready_gate(self):
        with self.assertRaisesRegex(RuntimeError,'OBSERVATION_INCOMPLETE_STOPPED'):
            recovery.continuous_ready(dict(at_limit=False,complete=False,records=[]),{}, {},0)


if __name__=='__main__':unittest.main()
