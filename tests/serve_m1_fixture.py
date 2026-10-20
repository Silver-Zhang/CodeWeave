"""Real, disposable CLI -> SQLite -> HTTP fixture for browser acceptance."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=18080)
    args = parser.parse_args()
    analyzer = Path(os.environ['CODEWEAVE_ANALYZER']).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix='codeweave-m1-browser-') as directory:
        work = Path(directory)
        source = work/'source'
        shutil.copytree(ROOT/'tests/fixtures/m1', source)
        left = source / 'left/unit.cpp'
        left.write_bytes(left.read_bytes().replace(b'\n', b'\r\n'))
        commands = [{'directory':str(source),'file':str(f),'arguments':['c++','-std=c++17','-I',str(source),'-c',str(f)]} for f in sorted(source.rglob('*.cpp'))]
        (work/'compile_commands.json').write_text(json.dumps(commands))
        command = [sys.executable,'-m','codeweave','index','--project-root',str(source),'--compilation-database',str(work),'--database',str(work/'index.sqlite3'),'--analyzer',str(analyzer)]
        if os.environ.get('CODEWEAVE_RESOURCE_DIR'):
            command += ['--resource-dir',os.environ['CODEWEAVE_RESOURCE_DIR']]
        for version in range(2):
            if version:
                (source/'left/unit.cpp').write_text('int changed_only() { return 99; }\n')
            result = subprocess.run(command,capture_output=True,text=True,timeout=60)
            if result.returncode:
                raise RuntimeError(result.stderr)
            print(json.dumps({'version':version,**json.loads(result.stdout)}),flush=True)
        import uvicorn
        from codeweave.app import create_app
        uvicorn.run(create_app(work/'index.sqlite3',ROOT/'frontend/dist'),host='127.0.0.1',port=args.port)

if __name__ == '__main__':
    main()
