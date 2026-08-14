# Security Policy

Persona Growth Loop reads conversation transcripts, distills persona phrases from them, and writes rendered overlay files through a deterministic, gate-checked pipeline. It runs no daemon and opens no listening ports (the remote-engine collector and deploy dispatcher make outbound SSH calls to a host alias you configure); its most security-sensitive surfaces are the scrub/filter layer that keeps personal data out of observation logs and the gates that keep writes inside the declared allowlist. Security reports are welcome for:

- Leaked credentials, tokens, or personal information anywhere in the repository or its git history
- Ways to get phrase bodies, secrets, or excluded speakers past the collector's content-block filter, scrub layer, or denylist
- Ways to make the applier, deploy dispatcher, or rollback path write outside the declared path allowlist or escalate beyond documented behavior
- Ways to defeat the killswitch, CP gates, soul-hash verification, or fail-closed paths while appearing compliant

## Reporting a Vulnerability

Please report security issues privately via **GitHub's private vulnerability reporting** on this repository (Security → Report a vulnerability). If that is unavailable, open a GitHub issue *without sensitive details* and ask a maintainer to establish a private channel.

We aim to acknowledge reports within 7 days. Please do not disclose the issue publicly until it has been addressed.

## Supported Versions

Only the latest tagged release and the `main` branch are maintained.
