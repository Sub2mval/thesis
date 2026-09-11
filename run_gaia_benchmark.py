"""Convenience entry point: `python run_gaia_benchmark.py [args...]`.
Run from the thesis-main/ directory. See gaia_runner/cli.py for all
available arguments, or pass --help.
"""

import sys

from gaia_runner.cli import main

if __name__ == "__main__":
    main(sys.argv[1:])