"""pa - self-owned personal assistants.

A small, hardened personal-assistant web app designed to run as a single
cloud service (Fly.io machine or a VM behind Caddy), with SQLite state,
a durable task ledger, guarded egress and one owner-pinned
OpenAI-compatible model endpoint (LLM_BASE_URL/LLM_MODEL; a dev-local
Ollama is allowed outside production). Discord is the main interface;
scheduler, Gmail (read-only) and Obsidian-vault retrieval are built in.
"""

__version__ = "1.0.0"
