"""Owned local worker entry; no imported scripts or shell commands."""
import argparse
from pathlib import Path
from codeweave.workspace import Workspace

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--request-id', required=True)
    args = parser.parse_args()
    Workspace(args.state).run(args.request_id)
