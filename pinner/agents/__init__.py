"""ADK agents (Phase 2): Moderator (multimodal vetting) + Strategist (SEO copy).

Both are google-adk LlmAgents with output_schema-constrained responses and
RunConfig(max_llm_calls) fencing. Product data is UNTRUSTED input: it enters
only user-role content, never the system instruction (prompt-injection
defense per the architecture review §1.4).
"""
