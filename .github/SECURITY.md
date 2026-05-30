# Security Policy

## Supported Versions

Security fixes are provided for the **latest released version** of NativMix.
Older releases may receive fixes on a best-effort basis only.

| Version | Supported          |
| ------- | ------------------ |
| latest  | :white_check_mark: |
| older   | :x:                |

Check the current release on [GitHub Releases](https://github.com/knoellix/NativMix/releases).

## Reporting a Vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

Preferred channels:

1. **[GitHub Security Advisories](https://github.com/knoellix/NativMix/security/advisories/new)** (private report — recommended)
2. Email: **moellix@knoellix.net** (PGP optional; ask if you need a key)

Include as much as possible:

- NativMix version and install method (AUR, OBS `.deb`/`.rpm`, Windows installer, source)
- OS and desktop environment
- Clear steps to reproduce
- Impact (e.g. local privilege escalation, arbitrary file write, remote code execution)
- Relevant log excerpts (redact personal paths if needed)

### What to expect

- **Acknowledgement** within a reasonable timeframe (typically within 7 days)
- **Status updates** as the report is triaged and fixed
- **Coordinated disclosure** — we prefer to release a fix before public details when feasible

## In Scope

Examples of reports we care about:

- Issues in NativMix's Python application code shipped in this repository
- Unsafe IPC/local socket handling that allows unauthorized control or data exposure
- Path or config handling that enables unintended file access outside XDG locations
- Packaging/installer issues that weaken system security (e.g. unsafe autostart or permissions)

## Out of Scope

The following are generally **not** handled as security advisories here:

- Bugs in third-party firmware (Arduino/deej sketches not maintained in this repo)
- Distro-specific packaging outside `packaging/` in this repository (report to the maintainer of that channel)
- Denial-of-service from misbehaving local hardware or MIDI devices
- Social engineering or physical access to an unlocked machine
- Issues already fixed on `main` but not yet released (still welcome — mention the branch/commit)

## Safe Harbor

We appreciate good-faith research. Do not access data that is not yours, disrupt other users' systems, or exceed what is needed to demonstrate a vulnerability.

Thank you for helping keep NativMix and its users safe.
