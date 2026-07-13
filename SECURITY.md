# Security Policy

LLM OS is a privacy-first, local-first system: the guarantees are the product.
This document states the threat model it defends and how to report a hole in it.

## Threat model

LLM OS runs on **one trusted machine, for one trusted user**, and binds only to
loopback. Within that boundary it defends:

- **Zero egress.** No component sends your data off the machine. A continuous
  egress sentinel watches the whole stack and writes any non-loopback
  connection to the audit chain; where its tooling is unavailable it reports
  *unavailable*, never *clean*.
- **The model proposes, a human approves.** Tools that change state are gated;
  a gated tool does not run until a person clicks Approve. On a single-user box
  that click is the human step. If untrusted local processes are in your threat
  model, `LLM_OS_APPROVAL_TOKEN=1` adds a per-boot token — printed only to the
  server console — that approval then requires, so a caller reaching only the
  HTTP API cannot approve its own proposal.
- **Tamper-evident audit.** Every action is hash-chained; the log can be
  verified by a standalone tool that trusts nothing in this repo.
- **Supply-chain pinning.** Model digests and MCP server file hashes are
  pinned; drift is refused at startup.
- **No training on your data.** Weights are frozen GGUF files, read-only at
  inference. Prove it: hash the weights, run a prompt, hash again.
- **Erasable.** `scripts/erase.py` clears every plaintext store and rotates
  the audit salt, making retained content cryptographically unrecoverable.

## Explicitly out of scope

- **Local malware already running as your user.** A process with your
  filesystem and network access can read the same data the app can; no
  loopback app can defend against that. The approval token raises the bar for
  an *HTTP-only* attacker (a rebound browser page, a network client), not for
  code already running as you.
- **Multi-user or networked deployment.** There is no authentication or
  authorization between users — the design assumes a single operator on
  localhost. Do not expose the port to a network.
- **Encryption at rest.** Data stores are plaintext on disk; the defense is
  erasure, not encryption. Full-disk encryption is your OS's job.

## Reporting a vulnerability

Email the maintainer rather than opening a public issue for anything that
affects one of the guarantees above. Include a reproduction if you can — this
project ships reproductions for the bugs it has fixed, and a good repro is the
fastest path to a fix.
