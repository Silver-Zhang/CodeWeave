"""Build CodeWeave itself and open a configured local workspace; never build the input project."""
import argparse
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]


def fail(message):
    raise SystemExit('CodeWeave startup: '+message)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--demo',action='store_true')
    parser.add_argument('--state-dir',type=Path,default=Path(os.environ.get('XDG_STATE_HOME',Path.home()/'.local/state'))/'codeweave/local')
    parser.add_argument('--project-root',type=Path)
    parser.add_argument('--compilation-database',type=Path)
    parser.add_argument('--head-compilation-database',type=Path)
    parser.add_argument('--dependency-root',type=Path,action='append',default=[])
    parser.add_argument('--toolchain-root',type=Path,action='append',default=[])
    parser.add_argument('--llvm-prefix',type=Path)
    parser.add_argument('--resource-dir',type=Path)
    parser.add_argument('--port',type=int,default=8000)
    parser.add_argument('--no-serve',action='store_true')
    args=parser.parse_args()
    if not 1<=args.port<=65535:fail('port must be 1..65535')
    for package in ('fastapi','uvicorn'):
        if importlib.util.find_spec(package) is None:
            fail(f'missing {package}; use a venv and pip install -e "{ROOT}/backend[dev]"')
    for command in ('cmake','git','bwrap','c++'):
        if not shutil.which(command):fail(f'missing {command}; install the matching local dependency (no automatic system installation)')
    prefix=args.llvm_prefix or (Path(os.environ['CODEWEAVE_LLVM_PREFIX']) if os.environ.get('CODEWEAVE_LLVM_PREFIX') else None)
    if prefix is None:
        llvm=shutil.which('llvm-config-18') or shutil.which('llvm-config')
        if llvm:prefix=Path(subprocess.check_output([llvm,'--prefix'],text=True).strip())
    if prefix is None:
        for candidate in (Path('/usr/lib/llvm-18'),ROOT.parent/'.toolchain/llvm-18/usr/lib/llvm-18'):
            if (candidate/'lib/cmake/clang/ClangConfig.cmake').is_file():prefix=candidate;break
    if prefix is None or not (prefix/'lib/cmake/clang/ClangConfig.cmake').is_file():
        fail('LLVM/Clang development packages missing; set --llvm-prefix or CODEWEAVE_LLVM_PREFIX to a matching installation')
    prefix=prefix.resolve()
    resource=args.resource_dir
    if resource is None:
        resources=list((prefix/'lib/clang').glob('*/include/stddef.h'))
        if len(resources)!=1:fail('set --resource-dir to the matching Clang directory containing include/stddef.h')
        resource=resources[0].parent.parent
    env={**os.environ,'PYTHONPATH':str(ROOT/'backend')+os.pathsep+os.environ.get('PYTHONPATH','')}
    node=shutil.which('node')
    if not node or tuple(map(int,subprocess.check_output([node,'-p','process.versions.node'],text=True).strip().split('.')[:2]))<(22,12):
        cached=ROOT.parent/'.toolchain/node22/node_modules/node/bin/node'
        if cached.is_file():node=str(cached)
        else:fail('Node >=22.12 is required to build the UI; put a supported Node on PATH')
    env['PATH']=str(Path(node).parent)+os.pathsep+env['PATH']
    print(f'Using Python {sys.executable}, LLVM {prefix}, Node {node}',flush=True)
    def run(argv,cwd=ROOT):subprocess.run(list(map(str,argv)),cwd=cwd,env=env,check=True)
    build=ROOT/'.build/analyzer'
    run(['cmake','-S',ROOT/'analyzer','-B',build,'-DCMAKE_PREFIX_PATH='+str(prefix),'-DCMAKE_BUILD_RPATH='+str(prefix/'lib')])
    run(['cmake','--build',build,'-j2'])
    if not (ROOT/'frontend/node_modules/.package-lock.json').is_file():run(['npm','ci','--no-audit','--no-fund'],ROOT/'frontend')
    run(['npm','run','build'],ROOT/'frontend')
    state=args.state_dir.absolute()
    project=args.project_root
    compilation=args.compilation_database
    if args.demo:
        if project or compilation:fail('--demo cannot be combined with a different project root/database')
        sys.path.insert(0,str(ROOT/'backend'))
        from codeweave.demo import create_demo
        project=create_demo(state/'demo-project',ROOT/'examples/workspace')
        compilation=project
    argv=[sys.executable,'-m','codeweave','open','--state-dir',str(state),'--frontend',str(ROOT/'frontend/dist'),'--port',str(args.port)]
    if project:
        if compilation is None:fail('a project root also requires --compilation-database')
        argv+=['--project-root',str(project),'--compilation-database',str(compilation),'--analyzer',str(build/'codeweave-analyzer'),'--resource-dir',str(resource)]
        for p in args.dependency_root:argv+=['--dependency-root',str(p)]
        for p in args.toolchain_root:argv+=['--toolchain-root',str(p)]
        if args.head_compilation_database:argv+=['--head-compilation-database',str(args.head_compilation_database)]
    elif not (state/'workspace.json').is_file():fail('first startup requires --demo or --project-root and --compilation-database')
    if args.no_serve:argv+=['--no-serve']
    print(f'Workspace state: {state}\nOpen http://127.0.0.1:{args.port} (Ctrl-C stops the service).',flush=True)
    os.execve(sys.executable,argv,env)


if __name__=='__main__':main()
