"""Portable evidence checks; Git-source verification uses a full clone separately."""
import importlib.util
import json
import pathlib
import shutil
import tempfile
import unittest

BUNDLE=pathlib.Path(__file__).resolve().parents[1]/'docs/hunter-v2-lifecycle-evidence/history-0f65e117'
spec=importlib.util.spec_from_file_location('hunter_history_verifier',BUNDLE/'verify_bundle.py')
verifier=importlib.util.module_from_spec(spec);spec.loader.exec_module(verifier)


class HistoryBundleTests(unittest.TestCase):
    def test_complete_frozen_evidence_bundle(self):
        result=verifier.verify(BUNDLE)
        self.assertEqual(result['status'],'PASS')

    def test_changed_manifest_counts_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            target=pathlib.Path(directory)/'bundle';shutil.copytree(BUNDLE,target)
            path=target/'manifest.json';data=json.loads(path.read_text(encoding='utf-8'))
            data['counts']['cycle_rows']-=1;path.write_text(json.dumps(data),encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'Manifest counts differ'):verifier.verify(target)

    def test_corrupt_compressed_cycles_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            target=pathlib.Path(directory)/'bundle';shutil.copytree(BUNDLE,target)
            path=target/'cycles.jsonl.gz';data=bytearray(path.read_bytes());data[-5]^=1;path.write_bytes(data)
            with self.assertRaisesRegex(ValueError,'Compressed hash mismatch'):verifier.verify(target)


if __name__=='__main__':unittest.main()
