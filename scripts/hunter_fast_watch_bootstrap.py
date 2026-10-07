"""Initialize a public read-only Git cache as the unprivileged service user."""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile

PUBLIC_REPOSITORY='https://github.com/leo14881-eng/btc-grid-state.git'

def initialize(repo):
    repo=Path(repo)
    env=dict(os.environ,GIT_TERMINAL_PROMPT='0')
    if not (repo/'.git').is_dir():
        if repo.exists():raise ValueError('READBACK_DIRECTORY_NOT_A_GIT_CACHE')
        repo.parent.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='readback-init-',dir=repo.parent) as temp:
            staging=Path(temp)/'repo'
            subprocess.run(['git','-c','credential.helper=','clone','--filter=blob:none',
                '--no-checkout',PUBLIC_REPOSITORY,str(staging)],check=True,env=env,timeout=120)
            staging.replace(repo)
    origin=subprocess.check_output(['git','-C',str(repo),'remote','get-url','origin'],text=True).strip()
    if origin!=PUBLIC_REPOSITORY:raise ValueError('PUBLIC_READBACK_ORIGIN_REQUIRED')
    subprocess.run(['git','-c','credential.helper=','-C',str(repo),'fetch','origin','main'],
        check=True,env=env,timeout=30)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--repo',required=True)
    initialize(parser.parse_args().repo)
