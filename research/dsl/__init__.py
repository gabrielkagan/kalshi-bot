"""Strategy DSL for Track A (Qwen-as-proposer).

Per kb/decisions/autoresearch-design-may05.md the DSL is bounded
YAML over already-computed features — Qwen cannot invent new
features, edit feature transforms, or write Python. Schema +
validation reject malformed proposals BEFORE replay (fast filter,
doesn't pollute results log).
"""
