"""Create the product's own demo once. Never modify an imported project."""
import json
from pathlib import Path
import shutil
import subprocess

from codeweave.index import IndexError
from codeweave.snapshot import ENV


def create_demo(destination: Path, examples: Path) -> Path:
    destination = destination.absolute()
    if destination.exists():
        if not (destination/'compile_commands.json').is_file():
            raise IndexError('destination_exists','demo destination already exists without its compilation database')
        return destination
    destination.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    shutil.copytree(examples,destination)
    commands=[{'directory':str(destination),'file':name,'arguments':['c++','-std=c++17','-I',str(destination),'-c',name]} for name in ('main.cpp','worker.cpp')]
    (destination/'compile_commands.json').write_text(json.dumps(commands,indent=2)+'\n')
    env={**ENV,'GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':'/dev/null','GIT_OPTIONAL_LOCKS':'0'}
    def git(*args):
        result=subprocess.run(['/usr/bin/git','-c','core.hooksPath=/dev/null','-c','commit.gpgsign=false',*args],cwd=destination,env=env,capture_output=True,text=True,timeout=30)
        if result.returncode:
            raise IndexError('demo_unavailable','cannot create owned demo Git baseline: '+result.stderr)
    git('init','-b','main')
    git('config','user.name','CodeWeave Demo')
    git('config','user.email','demo@example.invalid')
    git('add','--','main.cpp','worker.cpp','shared.hpp','config.hpp','README.md','compile_commands.json')
    git('commit','-m','Owned demo baseline')
    return destination
