# CLAUDE.md

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


## Project memory

This repository has a persistent memory that survives across machines and sessions.
It lives in an Obsidian vault reached through the `om` MCP server.

**Do not start substantive work in this repository without consulting it first.**
The last session left its reasoning there, and re-deriving it wastes the time this
layer exists to save.

At the start of a task:

1. Call `recall` вЂ” durable lessons scoped to this repo, most specific first. Call it
   with no query to see everything in scope; that is usually what you want first.
2. Read `projects/<this-repo>/README.md` through `search` or the `vault://` resource.
   It is the single source of this project's current status and next step.
3. If `recall` and `search` return the notes but not the answer вЂ” several notes bear
   on the question and they disagree, or the judgement spans them вЂ” use `reason`.

At a meaningful stopping point, before the context is lost:

4. Call `record_work`. Fill `changes`, `decisions`, `learned`, `verification` and
   `open`. A future session cannot reconstruct these from the diff, and a sparse
   record is near-worthless six weeks later, which is when it gets read.
5. If something you learned would still be true in a **different** repository, call
   `remember`. That is the test. A log of what you did today is `record_work`, not a
   memory вЂ” a memory store filling with status updates is worse than an empty one.
6. Say so if the status note is now out of date. It is the file that answers "where
   did we stop", and it is only worth reading if it is current.

If something you expect to be in the vault cannot be found, call `health` before
concluding it is not there. Every failure in this layer вЂ” a missing index, a
misconfigured root, an unresolved caller identity вЂ” presents identically as "no
results", and `health` is what distinguishes them.

Never write vault paths, vault contents, or session URLs into commits, PR
descriptions, or code comments in this repository.
