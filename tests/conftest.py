"""Add project root to sys.path so test files can import top-level modules."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
