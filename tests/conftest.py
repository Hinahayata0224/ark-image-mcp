"""Shared fixtures for offline tests (no live API calls)."""
import os
import sys

# Make the package importable without installing it.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("ARK_API_KEY", "test-key-placeholder")
