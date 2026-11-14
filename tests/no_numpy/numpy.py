"""Stands in for NumPy while Dolly's tests run with DOLLY_TEST_NO_NUMPY=1: importing it
fails, as on a stock Omarchy, where python-numpy is not installed. tests/conftest.py puts
this folder first on the module path."""
raise ImportError("NumPy is hidden in Dolly's tests (DOLLY_TEST_NO_NUMPY=1): Dolly must run without it")
