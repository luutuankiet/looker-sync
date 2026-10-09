#!/usr/bin/env python3
"""Run the CLI from a source checkout: python3 scripts/looker_sync.py ..."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src'))
from looker_sync.cli import main  # noqa: E402

if __name__ == '__main__':
    sys.exit(main())
