 Tier 1 — the adoption blockers (build these before launch)

  1. Streaming responses. Right now a user types a question and stares at "Thinking…" for 3–40 seconds with zero feedback. Every chat UI in existence streams tokens. This is the single biggest perceived-quality gap, and on a slow local model it matters more than it does for cloud AI — streaming makes a 7B model feel usable and a non-streaming one feel broken. POST /chat/stream (SSE) plus the tool-call trace emitted as events.

  2. A one-command install that includes the model. Today: clone, venv, pip, pull two models, launch. That's four ways to lose someone. curl -sSL … | sh or a make setup that checks for Ollama, pulls the models, builds the venv, pins the digests, and runs preflight. Every hour of friction here costs you stars.

  3. Session/conversation persistence. The kernel is stateless per request; the UI holds history in browser memory. Refresh and it's gone. A NOC engineer or lawyer needs "what did I ask yesterday, and what did the agent do?" — and you already have the audit chain to build it from. Conversations list + resume.

  Tier 2 — the capability gaps a real user hits in week one

  4. Document Q&A over their own files (RAG). This is the request from your target buyers — "can it read my contracts / my specs / my case files?" TelecomOS proved the pattern with search_specs; generalize it into the kernel as a built-in: a watched folder, local embeddings, cited answers. This is the feature that makes LLM OS useful to a lawyer without writing an MCP server.

  5. Tool approval gates in the kernel. You built propose→approve→execute for TelecomOS. That belongs in the platform: mark any tool requires_approval: true and the kernel refuses to execute it until a human confirms — with the confirmation in the audit chain. It's the generic version of your best idea, and it's what makes "give the agent write access to my filesystem" safe.

  6. Multi-turn context. The kernel currently sends system + memories + the single prompt. Follow-ups like "and cell 5?" or "summarize that as a table" fail. A bounded conversation window (last N exchanges) is a small change with a huge usability payoff — and it's arguably a bug, not a feature.

  Tier 3 — the differentiators that earn the "OS" name

  7. Scheduled/background agents — "every morning, sweep the network and write me a report." You have all the pieces (tools, memory, audit); what's missing is a scheduler. This is what turns an assistant into an operating system doing work while you sleep.
  8. Audit export & verification CLI — llm-os verify-audit audit.jsonl that an auditor can run standalone, plus signed exports. That's your SOC 2 / compliance artifact.
  9. Cost/latency telemetry (local) — tokens/sec, per-tool latency, model comparison in the UI. Cheap, and it feeds your eval story.
