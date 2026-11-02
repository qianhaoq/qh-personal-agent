"""Legacy extract-job import alias, never a production orchestration entry.

The implementation lives in workers/extract_impl.py. Kept to drain pre-runtime
extract rows and preserve existing queue lease regression tests.
"""
import sys

from app.workers import extract_impl

sys.modules[__name__] = extract_impl
