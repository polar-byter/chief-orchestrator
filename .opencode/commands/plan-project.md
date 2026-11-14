---
description: Plan a multi-module project — break it into modules, no code
agent: build
---

Plan the project `$ARGUMENTS`. Do NOT write any code and do NOT call any subagent.

Steps:
0. Resolve requirements using Task intake in AGENTS.md: use the user's chat description or read TASK.md yourself when the request refers to it. Do not require a file for a task already described in chat. Discover relevant project docs yourself.
   Follow docs/kit/DEVELOPMENT_METHOD.ru.md (v4). Capture scope and priorities for the project manager, examples and measurable acceptance criteria for the analyst, and architectural decisions for the engineering lead. Keep effort inputs, capacity and risk responses distinguishable. Reuse existing documents and preserve the complete requested scope.
1. Resolve and read the work plan using the **Work plan (memory)** section of AGENTS.md. Create it if missing; use the same resolved file throughout this task.
2. Break the task into modules. For each module give: name, responsibility, public interface (functions/classes), and dependencies on other modules.
3. Write the module checklist into that work plan as `- [ ]` items.
   On project onboarding, run `kit.project_docs init` with the project Python wrapper.
   Check `list` and use `add stage` and `add feature --parent` for the agreed
   stages and features. Use the same stable keys when extending this plan.
   Link each substantive work item to its requirement, dependencies and observable acceptance check. Include affected documentation and README updates in the work item.
4. Show me the plan and stop. Wait for my approval before any code. Explain that approval to implement the whole plan lets the Master Chief choose and build modules in dependency order without separate `/module` requests.

Output format:
- Modules: name — responsibility — interface — depends on
- Order of work: which module first and why
- Work plan: resolved path and checklist status
