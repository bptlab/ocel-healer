# Synthetic Evaluation: inject → detect → resolve

*Model:* `phi4:14b` (digest `ac896e5b8b34a1f4efa7b14d7520725140d5512484457fab45d2a4ea14c69dba`, Ollama 0.40.11) — *date:* 2026-11-21 19:02 — *runs per scenario:* 10 — *duration:* 381 min
*Commit:* `56c155732fce26a9c39793ef55753ed2bbcdab09` — *eval log sha256:* `24a563f345de` — *temperature:* 0.5 — *seed:* 0 — *LLM distractors:* 20 — *time tolerance:* 10:00:00 — *examination hints:* off

Each scenario corrupts **one fixed record** of a clean copy of the log; the runs repeat it and vary only in the LLM's answers and the distractors. The 95 % intervals (Wilson) therefore describe how stable the answers are, not how the method generalises over records.

**Detected**: the issue is reported (rule cells) or the injected candidate is flagged (LLM cells). **False alarms**: clean candidates of the same type, drawn afresh per run, flagged by the LLM; *per full sweep* = false-alarm rate × the number of clean candidates of that type. **Strict**: the repaired log equals the clean log (numbers ±10 %, timestamps within the tolerance); needs an applied repair. **Error** (median over repaired runs that are not strict): largest timestamp error, largest relative numeric error, wrong cells, rows missing or extra. **Invalid**: LLM replies that did not parse; left out of the rates. **Errors**: runs whose LLM call failed; left out of all rates.

## Rule detection, rule repair (no LLM involved)

| Cell | Issue | Diff. | Runs | Repaired | Strict | Error | Repair by | Collateral | New violations | Errors |
|---|---|---|---|---|---|---|---|---|---|---|
| Incorrect / Event | `duplicate_events_on_attributes` | easy | 10 | 10/10 | 10/10 (72–100 %) | — | rule 10 | — | — |  |
| Incorrect / Event | `duplicate_events_on_attributes` | hard | 10 | 10/10 | 10/10 (72–100 %) | — | rule 10 | — | — |  |
| Incorrect / Event | `duplicate_events_on_ids` | easy | 10 | 10/10 | 10/10 (72–100 %) | — | rule 10 | — | — |  |
| Incorrect / Event-to-Object | `duplicate_e2o_relations` | easy | 10 | 10/10 | 10/10 (72–100 %) | — | rule 10 | — | — |  |
| Incorrect / Event-to-Object | `duplicate_e2o_relations` | hard | 10 | 10/10 | 10/10 (72–100 %) | — | rule 10 | — | — |  |
| Incorrect / Object | `duplicate_objects_on_attributes` | easy | 10 | 10/10 | 10/10 (72–100 %) | — | rule 10 | — | — |  |
| Incorrect / Object | `duplicate_objects_on_attributes` | hard | 10 | 10/10 | 10/10 (72–100 %) | — | rule 10 | — | — |  |
| Incorrect / Object | `duplicate_objects_on_ids` | easy | 10 | 10/10 | 10/10 (72–100 %) | — | rule 10 | — | — |  |
| Incorrect / Object-to-Object | `duplicate_o2o_relations` | easy | 10 | 10/10 | 10/10 (72–100 %) | — | rule 10 | — | — |  |
| Incorrect / Object-to-Object | `duplicate_o2o_relations` | hard | 10 | 10/10 | 10/10 (72–100 %) | — | rule 10 | — | — |  |
| Incorrect / Object-to-Object | `o2o_self_loop` | easy | 10 | 10/10 | 10/10 (72–100 %) | — | rule 10 | — | — |  |
| Incorrect / Object-to-Object | `o2o_self_loop` | hard | 10 | 10/10 | 10/10 (72–100 %) | — | rule 10 | — | — |  |
| Missing / Event-to-Object | `dangling_e2o_relationship` | easy | 10 | 10/10 | 10/10 (72–100 %) | — | rule 10 | missing_object | — |  |
| Missing / Object-to-Object | `dangling_o2o_relationship` | easy | 10 | 10/10 | 10/10 (72–100 %) | — | rule 10 | — | — |  |
| Missing / Object-to-Object | `dangling_o2o_relationship` | hard | 10 | 10/10 | 10/10 (72–100 %) | — | rule 10 | — | — |  |

## Rule detection, LLM repair

| Cell | Issue | Diff. | Runs | Repaired | Strict | Error | Repair by | Collateral | New violations | Errors |
|---|---|---|---|---|---|---|---|---|---|---|
| Incorrect / Event | `duplicate_events_on_ids` | hard | 10 | 10/10 | 10/10 (72–100 %) | — | llm 10 | — | — |  |
| Incorrect / Event Attribute Type | `incorrect_event_attribute_datatype` | easy | 10 | 10/10 | 0/10 (0–28 %) | 11 % | rule 10, llm 10 | — | — |  |
| Incorrect / Event Attribute Type | `incorrect_event_attribute_datatype` | hard | 10 | 10/10 | 10/10 (72–100 %) | — | llm 10 | — | — |  |
| Incorrect / Object | `duplicate_objects_on_ids` | hard | 10 | 10/10 | 10/10 (72–100 %) | — | llm 10, rule 10 | — | — |  |
| Incorrect / Object Attribute Type | `incorrect_attribute_datatype` | easy | 10 | 10/10 | 0/10 (0–28 %) | 62 %, 5 cells | rule 10, llm 10 | — | — |  |
| Incorrect / Object Attribute Type | `incorrect_attribute_datatype` | hard | 10 | 10/10 | 0/10 (0–28 %) | 1 cells | llm 10 | — | — |  |
| Missing / Event | `missing_event` | easy | 10 | 10/10 | 8/10 (49–94 %) | 2437 min | llm 20 | dangling_e2o_relationship | — |  |
| Missing / Event | `missing_event` | hard | 10 | 10/10 | 0/10 (0–28 %) | 3360 min, 1 cells | llm 20 | dangling_e2o_relationship | — |  |
| Missing / Event Attribute Value | `missing_event_attribute_value` | easy | 10 | 10/10 | 0/10 (0–28 %) | 1 cells | llm 10 | — | — |  |
| Missing / Event Attribute Value | `missing_event_attribute_value` | hard | 10 | 10/10 | 0/10 (0–28 %) | 103 % | llm 10 | — | — |  |
| Missing / Event Time | `missing_event_timestamp` | easy | 10 | 10/10 | 0/10 (0–28 %) | 712 min | llm 10 | — | — |  |
| Missing / Event Time | `missing_event_timestamp` | hard | 10 | 10/10 | 0/10 (0–28 %) | 1541 min | llm 10 | — | — |  |
| Missing / Event Type | `missing_event_type` | easy | 10 | 10/10 | 10/10 (72–100 %) | — | llm 10 | — | — |  |
| Missing / Event Type | `missing_event_type` | hard | 10 | 10/10 | 10/10 (72–100 %) | — | llm 10 | — | — |  |
| Missing / Event-to-Object | `dangling_e2o_relationship` | hard | 10 | 10/10 | 10/10 (72–100 %) | — | rule 10, llm 10 | missing_event, missing_object | — |  |
| Missing / Object | `missing_object` | easy | 10 | 10/10 | 0/10 (0–28 %) | 184 % | llm 10 | dangling_e2o_relationship, dangling_o2o_relationship | — |  |
| Missing / Object | `missing_object` | hard | 10 | 10/10 | 0/10 (0–28 %) | 51 % | llm 10 | dangling_e2o_relationship, dangling_o2o_relationship | — |  |
| Missing / Object Attribute Value | `missing_attribute_value` | easy | 10 | 10/10 | 0/10 (0–28 %) | 62 %, 5 cells | llm 10 | — | — |  |
| Missing / Object Attribute Value | `missing_attribute_value` | hard | 10 | 10/10 | 0/10 (0–28 %) | 78 % | llm 10 | — | — |  |
| Missing / Object Type | `missing_object_type` | easy | 10 | 10/10 | 4/10 (17–69 %) | 1 cells | llm 10 | — | — |  |
| Missing / Object Type | `missing_object_type` | hard | 10 | 10/10 | 10/10 (72–100 %) | — | llm 10 | — | — |  |

## LLM detection, separate LLM repair

| Cell | Issue | Diff. | Runs | Detected | False alarms | per full sweep | Strict | Error | Invalid | Errors |
|---|---|---|---|---|---|---|---|---|---|---|
| Incorrect / Event Attribute Value | `incorrect_event_attribute_value` | easy | 10 | 10/10 (72–100 %) | 16/200 (5–13 %) | ≈ 320 of 3999 | 0/10 (0–28 %) | 72 % |  |  |
| Incorrect / Event Attribute Value | `incorrect_event_attribute_value` | hard | 10 | 10/10 (72–100 %) | 29/200 (10–20 %) | ≈ 1110 of 7658 | 0/10 (0–28 %) | 1 cells |  |  |
| Incorrect / Event Time | `incorrect_event_time` | easy | 10 | 10/10 (72–100 %) | 200/200 (98–100 %) | ≈ 1999 of 1999 | 0/10 (0–28 %) | 1685 min |  |  |
| Incorrect / Event Time | `incorrect_event_time` | hard | 10 | 10/10 (72–100 %) | 195/200 (94–99 %) | ≈ 1949 of 1999 | 0/10 (0–28 %) | 1669 min |  |  |
| Incorrect / Event Type | `incorrect_event_type` | easy | 10 | 5/10 (24–76 %) | 0/200 (0–2 %) | ≈ 0 of 7659 | 5/10 (24–76 %) | — |  |  |
| Incorrect / Event Type | `incorrect_event_type` | hard | 10 | 1/10 (2–40 %) | 0/200 (0–2 %) | ≈ 0 of 2000 | 1/10 (2–40 %) | — |  |  |
| Incorrect / Object Attribute Value | `incorrect_attribute_value` | easy | 10 | 10/10 (72–100 %) | 168/200 (78–88 %) | ≈ 117 of 139 | 0/10 (0–28 %) | 64 %, 5 cells |  |  |
| Incorrect / Object Attribute Value | `incorrect_attribute_value` | hard | 10 | 10/10 (72–100 %) | 174/200 (82–91 %) | ≈ 121 of 139 | 0/10 (0–28 %) | 41 % |  |  |
| Incorrect / Object Type | `incorrect_object_type` | easy | 10 | 10/10 (72–100 %) | 180/180 (98–100 %) | ≈ 18 of 18 | 10/10 (72–100 %) | — |  |  |
| Incorrect / Object Type | `incorrect_object_type` | hard | 10 | 10/10 (72–100 %) | 184/200 (87–95 %) | ≈ 18 of 20 | 10/10 (72–100 %) | — |  |  |

## LLM detection of missing attributes (schema)

| Issue | Diff. | Runs | Recall | Precision (names) | False alarms (types) | Strict (columns) | Invalid | Errors |
|---|---|---|---|---|---|---|---|---|
| `missing_event_attribute` | easy | 10 | 0/10 (0–28 %) | — | 0/100 (0–4 %) | 0/10 (0–28 %) |  |  |
| `missing_event_attribute` | hard | 10 | 10/10 (72–100 %) | 10/40 (14–40 %) | 30/100 (22–40 %) | 10/10 (72–100 %) |  |  |
| `missing_object_attribute` | easy | 10 | 10/10 (72–100 %) | 10/55 (10–30 %) | 22/50 (31–58 %) | 0/10 (0–28 %) |  |  |
| `missing_object_attribute` | hard | 10 | 9/10 (60–98 %) | 9/55 (9–28 %) | 23/50 (33–60 %) | 0/10 (0–28 %) |  |  |

## Totals

| Group | Runs | Detected | Strict |
|---|---|---|---|
| Rule detection, rule repair (no LLM involved) | 150 | 150/150 (98–100 %) | 150/150 (98–100 %) |
| Rule detection, LLM repair | 210 | 210/210 (98–100 %) | 82/210 (33–46 %) |
| LLM detection, separate LLM repair | 100 | 86/100 (78–91 %) | 26/100 (18–35 %) |
| LLM detection of missing attributes (schema) | 40 | 29/40 (57–84 %) | 10/40 (14–40 %) |

## Detector findings on the clean log

Rows the rule detectors report on the clean log before any injection. They are subtracted from every run, so they are not counted above; any listed here is a false positive of that detector.

- `duplicate_events_on_attributes`: 2
- `duplicate_objects_on_attributes`: 123
