# Context for ChatGPT

We are extending https://github.com/sagivn135/TradingAgents, forked from TauricResearch/TradingAgents, into a stock research/recommendation system.
Cursor and Codex handle repository implementation. LM Studio serves a local model via the existing openai_compatible provider. Read AGENTS.md and docs/WORKSPACE.md when available.
Help with requirements, source evaluation and architecture. Produce concrete specifications that can be committed to docs/. Distinguish sourced facts, assumptions and untested proposals. Do not assume this conversation is synchronized with Cursor/Codex or that you can access repository files unless a connector is actually available.
Current remaining setup: install/load a capable local chat model; configure exact model IDs; verify tool calling and one end-to-end analysis. Additional data sources and the product UI are not implemented yet.
