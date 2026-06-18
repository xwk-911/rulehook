from . import claude_code, codex, cursor  # noqa: F401

ADAPTERS = {
    "claude-code": claude_code,
    "codex": codex,
    "cursor": cursor,
}
