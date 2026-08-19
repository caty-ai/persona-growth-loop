.PHONY: test lint

test:
	python3 -m unittest discover -s tests

# lint is a deliberate no-op: no linter is configured in this repo yet.
# This target exists so the family-standard CI gate (`make test` / `make lint`)
# can land unmodified. Replace with a real linter invocation once one is adopted.
lint:
	@true
