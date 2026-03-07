"""Shared pytest fixtures for Kalshi bot test suite."""

import os
import sys

# Add project root to path so tests can import bot modules
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# Ensure venv is never collected
collect_ignore_glob = ["venv/*"]
