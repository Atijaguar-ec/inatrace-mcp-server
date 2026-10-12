"""
Test runner for INATrace MCP Server test suite.
Executes all unit and integration tests with discovery.
"""
import sys
import unittest
from pathlib import Path

# Ensure inatrace-mcp-server root is on sys.path
root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir))

def main():
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=str(Path(__file__).parent), pattern="test_*.py")
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)

if __name__ == "__main__":
    main()
