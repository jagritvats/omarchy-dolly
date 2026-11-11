"""Stands in for OpenCV while Dolly's tests run: importing it fails, as on a machine where
python-opencv is not installed. tests/conftest.py puts this folder first on the module path."""
raise ImportError("OpenCV is hidden in Dolly's tests (DOLLY_TEST_NO_CV2=1): Dolly must run without it")
