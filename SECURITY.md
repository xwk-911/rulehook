# Security Policy

## What rulehook is and is not

rulehook is a **behavioral guardrail**, not a security boundary:

- On Codex, Pre/PostToolUse hooks currently intercept only the Bash tool, and
  the model can route around them (documented by OpenAI). Combine rulehook with
  sandboxing and least-privilege permissions for security-critical enforcement.
- The LLM judge can itself be influenced by adversarial tool output
  (prompt injection). Deterministic `pattern_only` rules are not subject to
  this; use them as the floor for must-never-happen rules.
- `fail_open = true` (default) means judge outages allow actions through.
  Set `fail_open = false` for stricter posture.

## Reporting a vulnerability

Please open a private security advisory on GitHub (Security → Advisories →
Report a vulnerability) rather than a public issue. We aim to respond within
7 days.
