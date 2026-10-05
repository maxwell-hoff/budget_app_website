# Agent prompt for running one step

Open `budget_app.code-workspace` (in `personal_projects_local/`) so the agent can see
both repos. Start a new agent chat, paste the prompt below, and replace `<STEP_ID>`
(for example `13a`) and optionally `<NOTES>`. Steps run in number order; the next
one is the lowest-numbered step in PLAN.md that is not `done`.

```text
You are working on the "paid app" project (docs in docs/paid_plaid/), which spans two repos:
- Website/server: /Users/maxhoff/personal_projects_local/budget_app_website
- Desktop app:    /Users/maxhoff/personal_projects_local/budget_app

1. Read budget_app_website/docs/paid_plaid/PLAN.md and API_CONTRACT.md first,
   including the Decisions log and the latest Handoff notes.
2. Do ONLY step <STEP_ID>. Confirm its "Depends on" steps are done; if not, stop and tell me.
3. Work on a new branch in the step's repo: feature/mhoff/<short_topic>_<yyyymmdd>.
4. Keep everything shippable: new website features stay behind ACCOUNTS_ENABLED,
   desktop changes stay behind BUDGET_APP_CLOUD, and existing behavior must not change.
5. If you change or add an endpoint, update API_CONTRACT.md in the same change.
6. Add or update tests and run them. Meet the step's "Done when" criteria.
7. Before finishing, update PLAN.md: set the step status, add the PR link, record any
   new decisions in the Decisions log, and write a Handoff notes entry
   (what was done, anything left, the next step).
8. Summarize what changed and any manual actions I need to take (env vars, dashboards, deploys).
9. Give me step-by-step live test instructions for this step (exact commands, URLs, and
   what I should see), and run as much of that live test yourself as you can.

Extra context for this step (optional): <NOTES>
```

## Tips

- Run one step per chat. Starting fresh keeps the agent focused on the plan, not on
  leftover context.
- For bigger steps, start the chat in Plan mode, review the plan, then let it build.
- Desktop steps (14, 14a, 15, 16, 20) change the desktop repo, but PLAN.md lives in the website
  repo. Commit the PLAN.md update on a matching branch in the website repo, or merge it
  directly to `main` since it is docs only.
- Manual steps (3, 18) and the "Before starting (manual)" notes on steps 7, 8, and 11
  are for you. Steps 3 and 18 can still use the prompt: the agent prepares config and a
  checklist, and you do the dashboard work.
