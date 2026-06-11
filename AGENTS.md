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

# [HDE_bot] recent context, 2026-06-11 8:37pm GMT+3

Legend: 🎯session 🔴bugfix 🟣feature 🔄refactor ✅change 🔵discovery ⚖️decision 🚨security_alert 🔐security_note
Format: ID TIME TYPE TITLE
Fetch details: get_observations([IDs]) | Search: mem-search skill

Stats: 50 obs (20 550t read) | 313 092t work | 93% savings

### Jun 3, 2026
3671 10:18a 🔵 Database schema mismatch; pre-SLA notifications stopped May 19; zero sends since then
3672 10:19a 🔵 Domain rocket1208.com is not in DNS (NXDOMAIN); HDE webhooks cannot resolve bot endpoint
3673 " 🔵 Bot configured for hde-bot.duckdns.org webhook, not rocket1208.com; user worked on wrong domain
3674 10:20a 🔵 Webhook infrastructure is working; hde-bot.duckdns.org resolves correctly and is reachable; HDE event configuration is missing
3675 10:24a 🔵 Pre-SLA notifications scheduled but never sent for ticket 171623
3676 10:27a 🔵 VPS missing automated DuckDNS IP update mechanism
3677 " 🔵 Confirmed: No DuckDNS IP updater cron job despite sudo access
3678 10:28a 🔵 DuckDNS integrated with ACME/Let's Encrypt but missing IP update daemon
3679 10:29a 🔵 DuckDNS token configured only for Let's Encrypt renewals, not IP updates
3680 10:30a 🔵 Let's Encrypt uses HTTP-01 (webroot), not DuckDNS DNS validation
3681 10:31a ✅ Documented correct DNS procedure: update DuckDNS on VPS IP change, not rocket1208.com
3682 10:37a 🟣 Implemented automated DuckDNS IP updater with systemd timer
3683 " 🔵 Pre-SLA timer cleared when staff reply is newer than last client reply
3684 10:38a 🔵 Pre-SLA verification checks if operator actually posted in HDE after client reply
3685 11:11a ⚖️ Approve Approach B: verify operator identity in staff_reply webhook to preserve pre-SLA timer
3686 11:20a 🔵 Regression tests for HDE bot pre-SLA timing and concurrent operation bugs
3687 11:22a ✅ Implementation plan created for pre-SLA autoreply gate fix
3688 11:24a 🔵 Pre-SLA timer clearance logic found in topic_manager.py _handle_staff_reply_locked
3689 11:26a ✅ Three regression test cases added to test_topic_manager.py for operator verification gate
3690 " 🔵 TDD Step 2 complete: three new tests fail as expected before implementation
3691 " ✅ TDD Step 3 complete: operator verification gate implemented in topic_manager.py
3692 " 🔵 TDD Step 5: Full test suite shows existing test regressions with new operator verification gate
3693 11:27a ✅ TDD Step 5 fixed: test_staff_reply_clears_pre_sla updated for backward compatibility with operator verification gate
3694 " 🔵 TDD implementation complete: 18 tests passing, operator verification gate fully implemented and tested
3695 " ✅ Pre-SLA operator verification gate implementation committed to fix/presla-ignore-autoreply branch
3710 11:45a 🔵 HDE Bot Complexity Analysis: 6 Priority Optimization Targets Identified
3751 12:26p 🔵 Mapped HDEApiClient instantiation patterns across codebase
3752 12:29p 🔵 Identified duplicated client usage patterns in topic_manager.py
3753 " 🔵 operator_replies.py uses single-call client pattern across four handlers
S735 Implement connection pooling optimization for HDE API client to reduce TCP+TLS handshake overhead in bulk operations (Priority 4 from complexity analysis) (Jun 3, 12:29 PM)
3760 12:33p 🔵 Performance bottlenecks identified in HDE Telegram bot complexity analysis
3761 12:34p 🔵 Test suite baseline: 4 failures identified in 221-test suite
3762 12:35p 🟣 Implement shared aiohttp connection pool for HDE API client
3763 12:37p 🔄 Wire shared connector pool to all HDE API ClientSession instances
3764 " 🔵 Connector pool wiring verified: all ClientSession calls updated
3765 " ✅ Add test fixture to reset HDE connector pool between tests
3766 " ✅ Integrate connector pool shutdown into bot lifecycle
3767 " ✅ Reorder connector pool shutdown in main.py lifecycle
3768 12:38p 🟣 Add test coverage for shared connector pool behavior
S737 Deploy TCP connection pool optimization (perf: share a TCP connection pool across requests) to production (Jun 3, 12:38 PM)
3803 1:02p 🔄 TCP connection pool sharing for HDEApiClient
3804 1:03p ✅ TCP connection pool optimization deployed to origin/main
3805 " 🔵 SSH access to production host confirmed
3807 " 🔵 HDE Bot service found running on production host
3808 " 🔵 HDE Bot service deployment path and configuration
3809 " ✅ TCP connection pool optimization deployed to production
3811 " ✅ HDE Bot service restarted with new TCP connection pooling code
S808 Detailed explanation of planned optimizations for bot commands and what improvements they will deliver (Jun 3, 1:03 PM)
### Jun 11, 2026
S809 Implement and complete optimization Point #1 for /aiimport command (batch dedup checking + deferred wiki building phase) (Jun 11, 6:25 PM)
S810 Complete Point #2 optimization for `/aianalyze` command: implement in-memory pattern deduplication to eliminate O(n²) database query behavior (Jun 11, 6:30 PM)
S811 Deploy Point #2 optimization (`/aianalyze` in-memory pattern caching) to production and assess remaining optimization opportunities from the complexity report (Jun 11, 6:37 PM)
S812 Evaluate remaining two optimization opportunities (#3 RAG search via heapq.nlargest, #6 parallel file downloads) and present tradeoffs to determine implementation priority (Jun 11, 6:41 PM)
S813 Performance optimizations for vector similarity search and concurrent attachment downloads in HDE bot; user completed two commits and awaiting deploy confirmation (Jun 11, 6:44 PM)
S814 Deploy and verify performance optimizations across HDE bot's AI processing pipeline (Jun 11, 7:03 PM)
4295 7:37p ✅ Deployed changes to main branch
4296 7:38p 🟣 Performance optimization deployed: concurrent ticket attachment downloads
S815 Measure real effect of shared HDE API connection pool; find bulk scenarios; compare connection creation overhead; add diagnostic tests; eliminate unnecessary client creation without changing behavior; deliver Russian-language report with concrete metrics and risks. (Jun 11, 8:25 PM)
4311 8:33p 🔵 HDE_bot complexity hotspots scanned and ranked
4312 8:34p 🔵 Manual inspection of HDE_bot complexity hotspots confirms context-dependent patterns
4313 " 🔵 HDE_bot test suite has 231 tests covering all major components

Access 313k tokens of past work via get_observations([IDs]) or mem-search skill.
</claude-mem-context>