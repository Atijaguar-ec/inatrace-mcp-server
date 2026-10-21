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


# Declared runtime dependencies. Each has an optional-import fallback in src/, so a
# missing one does not fail: it silently degrades the module under test (no FastMCP
# SDK, no asyncpg driver, no Shapely topology, no JWT verification). The suite would
# still report OK, which is how a certified 73/73 run never loaded any of them.
REQUIRED_MODULES = {
    "mcp": "mcp (FastMCP SDK)",
    "asyncpg": "asyncpg (PostgreSQL driver)",
    "shapely": "shapely (EUDR polygon topology)",
    "pyproj": "pyproj (WGS84 projections)",
    "jose": "python-jose[cryptography] (RS256 JWT verification)",
    "pydantic_settings": "pydantic-settings (.env loading)",
}


def check_dependencies():
    import importlib
    missing = []
    for module, label in REQUIRED_MODULES.items():
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(label)

    if missing:
        print("=" * 70)
        print("ABORTADO: faltan dependencias declaradas en pyproject.toml")
        print("=" * 70)
        for label in missing:
            print(f"  - {label}")
        print()
        print("Sin ellas, src/ activa sus rutas degradadas y la suite pasaría sin")
        print("ejercitar el SDK de MCP, el driver de PostgreSQL, la topología de")
        print("Shapely ni la verificación de firmas JWT.")
        print()
        print("Instale el entorno del proyecto:  uv pip install -r pyproject.toml")
        print("=" * 70)
        sys.exit(2)


def main():
    check_dependencies()
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=str(Path(__file__).parent), pattern="test_*.py")
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)

if __name__ == "__main__":
    main()
