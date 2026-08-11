# Research basis

Snapshot: 2026-07-09. This reference records the primary and official sources used to design the workflow taxonomy. Anthropic material supplies architecture examples only; it does not authorize selecting an Anthropic model.

## Cross-cutting workflow patterns

- [OpenAI, A practical guide to building agents](https://openai.com/business/guides-and-resources/a-practical-guide-to-building-ai-agents/) distinguishes deterministic workflows from model-directed agents, recommends maximizing a single agent before adding multi-agent overhead, describes manager and handoff patterns, and recommends establishing a best-model baseline before replacing phases with smaller models under evals.
- [Anthropic, Building effective agents](https://www.anthropic.com/engineering/building-effective-agents) defines prompt chaining, routing, parallel sectioning or voting, orchestrator-workers, evaluator-optimizer, and autonomous tool loops. Its core warning is to use the simplest architecture that produces measured value.
- [Google ADK, Template agent workflows](https://adk.dev/agents/workflow-agents/) documents deterministic sequential, parallel, and loop control. Its 2026 ADK 2.0 guidance points complex systems toward graph and dynamic workflows.
- [Google ADK, Workflow patterns](https://adk.dev/workflows/patterns/) covers coordinator/dispatcher, sequential pipelines, fan-out/gather, hierarchical decomposition, generate-and-review, iterative refinement, and human input.
- [Google ADK 2.0](https://adk.dev/2.0/), [graph workflows](https://adk.dev/graphs/), [dynamic workflows](https://adk.dev/graphs/dynamic/), and [collaborative workflows](https://adk.dev/workflows/collaboration/) separate deterministic graph structure, programmatic dynamic control, and open-ended delegation. Python 2.0 became GA on 2026-05-19 and Go 2.0 on 2026-06-30.
- [Google, Why we built ADK 2.0](https://developers.googleblog.com/en/why-we-built-adk-20/) was published 2026-07-01 and argues for hybrid workflows: known business logic stays deterministic while agent nodes handle ambiguity.
- [LangGraph, Workflows and agents](https://docs.langchain.com/oss/python/langgraph/workflows-agents) implements chaining, parallelization, routing, orchestrator-workers, evaluator-optimizer, and agent loops. [LangGraph overview](https://docs.langchain.com/oss/python/langgraph/overview) emphasizes durable execution, persistence, human input, and observability for long-running stateful work.
- [LangChain, Multi-agent patterns](https://docs.langchain.com/oss/python/langchain/multi-agent/index) distinguishes subagents, handoffs, skills, routers, and custom workflows. Its router guidance favors clear categories and parallel source queries; supervisors fit evolving, conversation-aware orchestration.
- [Microsoft Agent Framework, Workflow orchestrations](https://learn.microsoft.com/en-us/agent-framework/workflows/orchestrations/) documents sequential, concurrent, handoff, group chat, and manager-led Magentic orchestration with human approval surfaces.

These sources converge on seven primitives used by the catalog: fixed chaining, classification/routing, fan-out/gather, dynamic decomposition, evaluator/refinement loops, human gates, and bounded autonomous loops.

## Coding and review

- [OpenAI Codex use cases](https://developers.openai.com/codex/use-cases) groups current workflows across codebase understanding, implementation, PR review, bug triage, data, documents, security, and verified operations.
- [GitHub, Managing issues and pull requests with Copilot](https://docs.github.com/en/copilot/how-tos/github-copilot-app/managing-issues-and-pull-requests) uses issue context, plan review, implementation, PR inspection, CI, comments, and human merge judgment.
- [GitHub, Cloud agent risks and mitigations](https://docs.github.com/en/copilot/concepts/agents/cloud-agent/risks-and-mitigations) describes branch isolation, draft PRs, restricted merge authority, checks, logs, and prompt-injection/data-leakage risks.
- [Anthropic, Harness design for long-running application development](https://www.anthropic.com/engineering/harness-design-long-running-apps), published 2026-03-24, reports a planner-generator-evaluator structure, tractable chunks, and structured artifacts across long sessions.
- [Anthropic, Effective harnesses for long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents) uses initializer state, incremental coding sessions, testing, commits, and durable handoffs.
- [OpenAI Codex PR review](https://learn.chatgpt.com/codex/use-cases/github-code-reviews) and [GitHub Copilot code review](https://docs.github.com/en/enterprise-cloud@latest/copilot/concepts/agents/code-review) both position automated review as evidence for human review, not a replacement for it.

Design result: coding is a verified change pipeline; exact tests are deterministic, diagnosis and review are separate cognitive phases, and external PR creation is split into preparation, authority, parent execution, and polling.

## Research

- [OpenAI, Introducing deep research](https://openai.com/index/introducing-deep-research/) was published 2025-02-02 and updated 2026-02-10 with MCP/app sources, trusted-site restriction, real-time progress, and interruption/refinement.
- [OpenAI Academy, Deep research](https://academy.openai.com/public/clubs/work-users-ynjqu/resources/deep-research), updated 2026-05-29, describes source-heavy consulting, strategy, finance, and legal work with traceable cited reports.
- [OpenAI Deep Research API guide](https://developers.openai.com/api/docs/guides/deep-research) separates clarification and prompt rewriting from the research run, exposes tool history and citations, and supports background execution plus tool-call bounds.
- [Anthropic, How we built our multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system), published 2025-06-13, uses a lead planner, parallel search subagents, and synthesis. It reports strong benefits for breadth-first questions but about 15 times chat token use and poor fit for tightly dependent work.
- [Google Gemini Deep Research Agent](https://ai.google.dev/gemini-api/docs/deep-research) uses collaborative planning, background search/read/reason loops, cited synthesis, explicit time/tool costs, and warnings about injection, citation verification, and combining private files with the open web.

Design result: freeze the question and source policy, fan out independent lanes, normalize evidence, synthesize once, adjudicate material conflicts, and verify claim-source entailment. A/B arms use the same captured source bundle.

## Planning and decisions

- [Microsoft Agent Framework, Magentic orchestration](https://learn.microsoft.com/en-us/agent-framework/workflows/orchestrations/magentic), updated 2026-05-26, uses manager planning, optional human plan review, specialist work, a progress ledger, stall detection, bounded replanning, and final synthesis.
- [OpenAI's use-case catalog](https://learn.chatgpt.com/use-cases) separates research, strategic scenarios, tradeoff memos, analytics scoping, and action workflows, reinforcing that decision support does not grant decision authority.
- OpenAI and Anthropic's cross-cutting guides reserve orchestrator-workers for tasks whose subtasks cannot be fully known in advance and evaluator loops for work with clear criteria.

Design result: make objectives and decision ownership explicit, compare materially distinct options under one rubric, preserve dissent and uncertainty, stress-test, and require an owner gate for consequential decisions.

## Data analysis and writing

- [OpenAI Codex, Analyze datasets and ship reports](https://learn.chatgpt.com/codex/use-cases/datasets-and-reports) follows inventory, schema and join inspection, reproducible transforms, analysis, visualization, and report artifacts; it warns against inventing missing values or merge keys.
- [Google BigQuery data canvas](https://cloud.google.com/bigquery/docs/data-canvas) uses a DAG from asset discovery through SQL, transformation, visualization, insights, and optional persistence, with explicit schema and security cautions.
- [Microsoft Agent Framework, Agents in workflows](https://learn.microsoft.com/en-us/agent-framework/workflows/agents-in-workflows), updated 2026-04-02, demonstrates writer-reviewer pipelines with streamed state.
- Anthropic's effective-agents guide uses outline-to-draft chains and evaluator-optimizer loops for writing when criteria are articulable.

Design result: data workflows preserve provenance and originals, validate before interpretation, keep transforms reproducible, and inspect rendered visuals. Writing workflows freeze the brief and sources, use a bounded critique loop, fact-check, and separate publication authority.

## Operations and incidents

- [OpenAI Codex, Run verified operations](https://learn.chatgpt.com/codex/use-cases/verified-operations-workflows) uses normalized inputs, policy/approval lookup, dry run, approved-scope execution, per-item outcomes, bounded transient retry, independent verification, and pauses before irreversible work.
- [Google SRE, Managing incidents](https://sre.google/sre-book/managing-incidents/) separates incident command, operations, communications, and planning, with one mutation lane, living state, handoffs, recovery verification, and preserved evidence.
- [Google Cloud, Data incident response](https://cloud.google.com/docs/security/incident-response), updated June 2026, moves from detection and triage through investigation, containment/recovery, communication, lessons, and prevention with named ownership.
- [GitHub deployment environments](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments) provide reviewer, branch/tag, wait, observability, security, secret, and concurrency gates.

Design result: preflight and dry-run first, authority is explicit, the parent executes through one mutation owner, polling is deterministic, verification is independent, and variance diagnosis never silently becomes rollback authority.

## Evaluation and self-improvement

- [Anthropic, Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents), published 2026-01-09, distinguishes tasks, trials, graders, transcripts, outcomes, harnesses, capability suites, and regression suites. It recommends code, model, and human graders, multiple trials, transcript review, balanced cases, and ongoing suite maintenance.
- [Karpathy, autoresearch](https://github.com/karpathy/autoresearch), published March 2026, demonstrates a fixed-time experiment loop: state a hypothesis, run a fixed evaluator, keep improvements, discard regressions, and preserve an experiment log.

Design result: workflow and model optimization are separate experiments. Model comparisons freeze the workflow; workflow comparisons freeze the model policy. Outcome graders outrank self-reports, held-outs protect generalization, safety failures veto gains, and live mutations are never duplicated for an A/B test.
