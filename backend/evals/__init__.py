"""Live-model eval suite for the embedding-backed capabilities.

A committed, re-runnable harness that verifies the two embedding consumers —
long-term memory (``MemoryStore.recall``) and cross-chat search
(``ConversationSearch.search``) — actually help the LLM at inference, against
**live** embedding + chat models. Everything here is gated behind the
``live_models`` pytest marker and auto-skips when the ``HEPH_EVAL_*`` env vars are
unset, so the default ``uv run pytest`` (credential-free CI) never touches it.

Three failure points are measured for each consumer:

- **Trigger** — does the agent call the retrieval tool when the question needs it?
- **Retrieval** — do the embeddings + RRF return the right item (over keyword)?
- **Grounding** — does the model use the recalled content in its answer?

Part A (``test_retrieval``) is the LLM-free retrieval ablation; Part B
(``test_endtoend``) is the on/off end-to-end agent benefit; ``report`` runs both
and writes ``FINDINGS.md``. All corpora are **synthetic** (``dataset.py``) —
never read from ``data/``.
"""
