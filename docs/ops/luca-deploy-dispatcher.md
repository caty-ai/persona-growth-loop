# Luca deploy dispatcher — operator runbook (private)

The operational wiring runbook for the Luca deploy dispatcher (forced-command SSH
key setup, host-specific install checklist, operator boundaries for rollback and
manual operations) lives in the private ops repository.

The dispatcher implementation itself ships in this repository
(`vps/pgl-luca-dispatch`, `growthlane/deploy.py`) together with its test suite;
only the host-specific operating instructions are private.
