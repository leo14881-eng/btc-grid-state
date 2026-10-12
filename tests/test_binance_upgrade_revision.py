"""Small adapter boundary tests; no filesystem, service or network operations."""
import contextlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock,patch

from scripts import hunter_binance_upgrade_revision as revision


class RevisionTests(unittest.TestCase):
    def args(self):
        return ['--repo','fixture','--expected-old',revision.PAIR[0],'--expected-new',revision.PAIR[1]]

    def test_plan_does_not_load_or_execute_upgrade(self):
        output=io.StringIO()
        with patch.object(revision,'load') as load,contextlib.redirect_stdout(output):revision.main(self.args())
        load.assert_not_called();value=json.loads(output.getvalue())
        self.assertEqual((value['old'],value['new']),revision.PAIR)
        self.assertFalse(value['start']);self.assertFalse(value['recovery'])

    def test_wrong_pair_fails_before_git_or_files(self):
        args=self.args();args[3]='99f75edcf2c2f95d59e5cf797051442639dc64ce'
        with patch.object(revision,'load') as load,self.assertRaisesRegex(ValueError,'UNREVIEWED_RELEASE_PAIR'):
            revision.main(args+['--apply'])
        load.assert_not_called()

    def test_unverified_helper_cannot_execute(self):
        with self.assertRaisesRegex(ValueError,'ENTRY_HASH_MISMATCH'):
            revision.configure(b'raise AssertionError("executed")',*revision.PAIR)

    def test_each_action_is_forwarded_without_added_recovery(self):
        previous=sys.argv
        for action in (['--check'],['--apply'],['--rollback','fixture-evidence']):
            module=Mock();seen=[];module.main.side_effect=lambda:seen.append(sys.argv[:])
            with patch.object(revision,'load',return_value=module) as load:
                revision.main(self.args()+action)
            load.assert_called_once_with(Path('fixture'),*revision.PAIR)
            self.assertEqual(seen,[['verified-upgrade','--repo','fixture']+action])
            self.assertIs(sys.argv,previous)


if __name__=='__main__':unittest.main()
