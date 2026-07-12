# AGENTS.md

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.


<claude-mem-context>
# Memory Context

# [HDE_bot] recent context, 2026-06-12 12:55pm GMT+3

Legend: 🎯session 🔴bugfix 🟣feature 🔄refactor ✅change 🔵discovery ⚖️decision 🚨security_alert 🔐security_note
Format: ID TIME TYPE TITLE
Fetch details: get_observations([IDs]) | Search: mem-search skill

Stats: 50 obs (17 254t read) | 185 987t work | 91% savings

### Jun 12, 2026
S867 Architectural audit of HDE_bot Telegram support bot: evaluate RAG implementation correctness, assess LLM integrations (Gemini/Groq/OpenRouter), determine if free LLM API consolidation to Groq + backup is feasible, research best practices for 2025-2026 (Jun 12, 1:27 AM)
S868 Prioritized action plan for HDE_bot improvements: explained 4 recommendations (confidence thresholding in RAG, provider inversion, reranking, refactoring) with effort estimates, risk assessment, and expected impact for user selection and implementation (Jun 12, 1:33 AM)
S869 Implement recommendation #1 (confidence threshold in RAG retrieval) from architectural audit; completed empirical analysis, implemented filtering, tested, and deployed to production VPS (Jun 12, 1:39 AM)
S870 Architectural review and migration: replace Gemini with Groq as primary LLM provider across all subsystems; verify RAG implementation and identify best practices to apply (Jun 12, 1:47 AM)
4590 8:59a ✅ Finalized Gemini Removal with Provider-Agnostic Documentation and Full Test Verification
4591 9:00a 🔵 Remaining Gemini References Found in Test Files
4592 " 🔵 Identified Gemini References in Test Suite Context
4593 " 🔵 Groq Scout Vision API Call Failed Due to Malformed Authorization Header
4594 9:01a 🔵 Groq Scout Model Vision Request Rejected with 403 Forbidden
4595 " 🔵 Groq Scout Vision API Supports Images but Requires Minimum Dimensions
4597 9:02a ✅ Gemini-to-Groq Migration Changes Staged for Commit
4599 9:03a 🔵 Gemini Migration Diff Summary: 20 Files with Primarily Provider-Related Changes
4600 " 🟣 Gemini Removal and Groq Consolidation Committed to Main Branch
4602 9:04a ✅ Updated Prompt Optimization Memory Documentation for Groq Migration
S871 How is Deepgram audio call transcription currently implemented in the project, and how can it be used for RAG to improve prompt quality and responses for clients? (Jun 12, 9:05 AM)
4603 9:09a 🔵 Deepgram audio transcription integration in ai_summary.py
4604 9:10a 🔵 Deepgram audio transcripts injected into RAG context for Groq Scout fallback
4605 " 🔵 Vision photo descriptions integrated into RAG embeddings as Priority 2 context
4606 " 🔵 RAG design spec documents Deepgram call transcription architecture and wiki integration
4607 9:11a 🔵 Deepgram workflow spec details operator call recording ingestion with wiki builder integration
4608 " 🔵 Audio media handling infrastructure exists; full transcription → wiki workflow not yet implemented
S872 Implement call recording transcription pipeline: audio file upload in ticket topics → Deepgram transcription → Groq summarization → knowledge base indexing as source='transcription' (Jun 12, 9:11 AM)
4609 9:28a 🟣 Retrieval evaluation utility for offline relevance judging
4610 9:29a 🔵 Retrieval dump executed on VPS with 30 optimization samples
4611 9:34a 🔵 find_similar() returns duplicate content, wasting top-k slots
4612 " 🔴 Implement content deduplication in find_similar() RAG search
4613 " 🔵 Test suite passes with deduplication fix integrated
4614 9:35a ✅ RAG deduplication fix deployed to production
4615 " 🔵 RAG scoring threshold calibrated on 30 tickets, achieves 0.88–0.96 for relevant results
4616 " 🔵 Data cleaning pipeline removes boilerplate before embedding to focus on problem semantics
4617 " 🟣 Vision-RAG augments knowledge items with photo descriptions before indexing
4618 9:36a 🔵 Automatic media attachment caching on all topic messages via universal message handler
4619 " 🔵 OperatorTopicContext and PreparedOutboundMessage encapsulate operator reply routing and authorization
4620 " 🔵 Multi-model LLM routing with Groq primary and OpenRouter fallback via Protocol pattern
4621 " 🔵 LLM concurrency controlled by shared asyncio.Semaphore across Groq and Deepgram calls
4622 " 🔵 Supported audio file types for speech-to-text processing
4623 9:37a 🔵 Groq summary API calls configured with low temperature (0.3) and 3000 token limit
4624 " 🔵 Image and audio attachment processing with fault isolation and strict size limits
4625 " 🔵 TicketTopic tracks SLA notifications, reassurance messages, and reply lifecycle
4626 " 🔵 Groq summary model is llama-3.3-70b-versatile (not base llama-3.3-70b)
4627 9:38a 🔵 Call recording pipeline: Deepgram transcription → Groq summary → knowledge indexing
4628 " 🔵 Extract_audio_meta distinguishes audio documents by filename extension when MIME type is generic
4629 9:39a 🟣 Transcription module implemented: Deepgram (nova-2) + Groq summarization pipeline
4630 " 🔵 Groq summarization uses 0.2 temperature (very low variance) vs. 0.3 for general summaries
4631 " 🟣 Transcription module tests pass: 15 test cases covering Deepgram and Groq integration
4632 " 🔵 Topic handler for call recordings enforces operator authorization and ticket linkage validation
4633 9:40a 🔵 Test suite expects handle_topic_call_recording handler in bot.handlers.commands (RED: 4 tests failing)
4634 " 🟣 Topic call recording handler implemented in bot.handlers.commands
S873 Deploy call recording transcription pipeline to production bot (Jun 12, 9:40 AM)
4636 9:47a 🟣 Call recording transcription pipeline implemented
4637 " 🟣 Call recording transcription pipeline with RAG integration
4639 " ✅ Call recording transcription feature pushed to production
4640 " ✅ Call recording transcription feature deployed to production VPS
4641 9:48a 🔵 Production bot started cleanly after transcription feature deployment
S874 Architectural study of HDE_bot project: evaluate RAG implementation, assess AI/LLM integrations, identify best practices, and plan LLM model replacement strategy (remove Gemini, optimize Groq, evaluate freellmapi) (Jun 12, 9:48 AM)
S875 User asked for a simple explanation of what changed and improved in the support bot system (Jun 12, 9:59 AM)
4669 12:09p 🔵 Environment field autofill uses LLM classification on ticket history
4670 " 🔵 Environment autofill only uses text conversation history, ignores available data sources
S877 Investigate how environment field autofill works in HDE ticket support bot, determine what data sources it uses, and propose improvement options (Jun 12, 12:10 PM)
4671 12:47p 🔵 Complexity Hotspots Identified in HDE Telegram Bot

Access 186k tokens of past work via get_observations([IDs]) or mem-search skill.
</claude-mem-context>