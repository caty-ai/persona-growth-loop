"""Pin the runtime version string for admission-bearing subprocess fixtures."""

import unicodedata


# CI runs Python 3.11/UCD 14, where the shipped runtime guard correctly refuses
# admission. Growth E2E tests that exercise behavior past that startup boundary
# use this test-only import hook; dedicated drift tests patch both directions.
unicodedata.unidata_version = "16.0.0"
