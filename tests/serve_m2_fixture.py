"""Owned real workspace for the Chromium edit/save/refresh acceptance loop."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import subprocess

ROOT=Path(__file__).resolve().parents[1]

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=18082);args=parser.parse_args()
    directory=Path(os.environ['CODEWEAVE_BROWSER_WORKDIR']);directory.mkdir(parents=True,exist_ok=True)
    from codeweave.demo import create_demo
    from codeweave.workspace import configure
    project=create_demo(directory/'project',ROOT/'examples/workspace')
    state=directory/'state'
    configure(state,project,project,Path(os.environ['CODEWEAVE_ANALYZER']),Path(os.environ['CODEWEAVE_RESOURCE_DIR']))
    command=[sys.executable,'-m','codeweave','workspace','refresh','--state-dir',str(state)]
    result=subprocess.run(command,capture_output=True,text=True,timeout=90)
    if result.returncode:raise RuntimeError(result.stderr)
    print(result.stdout,flush=True)
    import uvicorn
    from codeweave.app import create_app
    uvicorn.run(create_app(frontend=ROOT/'frontend/dist',workspace_state=state,allow_workspace_refresh=True),host='127.0.0.1',port=args.port)

if __name__=='__main__':main()
