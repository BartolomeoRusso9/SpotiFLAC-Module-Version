"""
Compatibility shim used by the test-suite.
Some tests import `backend.SpotiFLAC`; expose the main factory
from the real package so tests run without modifying test code.
"""

from SpotiFLAC import SpotiFLAC

__all__ = ["SpotiFLAC"]
