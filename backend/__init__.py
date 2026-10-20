"""Compatibility shim used by the test-suite.
Some tests import `backend.Mornye`; expose the main factory
from the real package so tests run without modifying test code.
"""

from mornye import Mornye

__all__ = ["Mornye"]
