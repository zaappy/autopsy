# Multi-Source Diagnosis — Verification Checklist (post-fix)

Re-verified after fixes: cross-source for any multi-collector prompt; deploy `files` fallback; terminal/Slack SOURCES only when multiple log sources **or** multiple deploy sources; post-mortem Data Sources table only when **>1** source.

---

## 1. AI PROMPT — MULTI-SOURCE LABELING (`prompts.py`)

| Item | Result |
|------|--------|
| `build_user_prompt()` groups by `data_type` | ✅ |
| Each source: `--- Source: {name} ---` | ✅ |
| CloudWatch vs Datadog separate | ✅ |
| GitHub vs GitLab separate | ✅ |
| Query, entry count, truncation per source | ✅ |
| Log lines: timestamp, level, message, `(×N)` occurrences | ✅ |
| Deploy: sha, ts, message, author; files + diff or **`files` fallback** | ✅ |
| Cross-source when **>1 collector** (`len(collected_data) > 1`) | ✅ |
| Cross-source lists active sources + correlate timestamps | ✅ |
| Single collector only → **no** cross-source | ✅ |
| No logs / no deploys messages | ✅ |

## 2. SYSTEM PROMPT

| Item | Result |
|------|--------|
| `SYSTEM_PROMPT_V1` extended (no V2); `prompt_version` v1 | ✅ |

## 3. DATA MODEL (`ai/models.py`)

| Item | Result |
|------|--------|
| `SourceInfo` + `DiagnosisResult.sources` default `[]` | ✅ |

## 4. ORCHESTRATOR (`diagnosis.py`)

| Item | Result |
|------|--------|
| `result.sources` populated; `--source` filter; parallel unchanged | ✅ |

## 5. TERMINAL (`terminal.py`)

| Item | Result |
|------|--------|
| SOURCES panel only when **>1 log source OR >1 deploy source** | ✅ |
| CloudWatch + GitHub (1+1) → **no** panel (backward compatible) | ✅ |
| Evidence not stripped | ✅ |

## 6. JSON (`json_out.py`)

| Item | Result |
|------|--------|
| Always includes `sources` array; full metadata | ✅ |

## 7. SLACK (`renderers/slack.py`)

| Item | Result |
|------|--------|
| Sources line only when **>1 log OR >1 deploy** (same rule as terminal) | ✅ |
| CW + GH → no extra Sources line | ✅ |

## 8. POST-MORTEM (`renderers/postmortem.py`)

| Item | Result |
|------|--------|
| **Data Sources** table only when `len(result.sources) > 1` | ✅ |
| Single source → no table | ✅ |

## 9. CLI

| Item | Result |
|------|--------|
| `--source` repeatable; TUI default no filter | ✅ |

## 10–12. TESTS

| Area | Result |
|------|--------|
| Prompt: single/multi/no logs/deploys/full/truncation/occurrences/**CW+GH cross-source**/**files fallback** | ✅ |
| Orchestrator: sources populated, filter single/multiple/invalid, CLI | ✅ |
| Renderers: panel multi/single/empty/**CW+GH no panel**, JSON sources | ✅ |
| Slack: multi log sources line; CW+GH no line | ✅ |
| Post-mortem: single no table; multi has table | ✅ |

## 13. BACKWARD COMPATIBILITY

| Item | Result |
|------|--------|
| CW + GitHub terminal: no SOURCES panel; Slack unchanged | ✅ |
| `--json` adds `sources` (unchanged otherwise) | ✅ |
| History saves full `DiagnosisResult` | ✅ |
| Collectors untouched; parallel unchanged; no new deps | ✅ |

## 14. CODE QUALITY

| Item | Result |
|------|--------|
| `from __future__ import annotations`, typing style | ✅ |

---

**Total: PASS on all checklist rows.**  
**Score: 100%** — ship.

Run: `pytest tests/ -v`
