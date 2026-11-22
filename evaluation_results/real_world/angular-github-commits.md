# Rule-based detection: `angular-github-commits`

*Log:* `data/real-world/angular-github-commits.sqlite` (179 MB) — *date:* 2026-11-22 17:16 — *detection runtime:* 8.4 s

Rows reported by each rule detector on the log as published (no ground truth, no LLM, no repair).

## Log

| Table | Rows |
|---|---:|
| event | 27,842 |
| object | 28,317 |
| event_object | 2,451,100 |
| object_object | 0 |
| event types | 67 |
| object types | 2 |

## Findings

| Row | Cell | Issue | Count |
|---|---|---|---:|
| Missing Data | Event | `missing_event` | 0 |
| Missing Data | Event Type | `missing_event_type` | 0 |
| Missing Data | Event Time | `missing_event_timestamp` | 0 |
| Missing Data | Event Attribute Value | `missing_event_attribute_value` | 18,370 |
| Missing Data | Object | `missing_object` | 0 |
| Missing Data | Object Type | `missing_object_type` | 0 |
| Missing Data | Object Attribute Value | `missing_attribute_value` | 0 |
| Missing Data | Object-to-Object | `dangling_o2o_relationship` | 0 |
| Missing Data | Event-to-Object | `dangling_e2o_relationship` | 0 |
| Incorrect Data | Event | `duplicate_events_on_ids` | 0 |
| Incorrect Data | Event | `duplicate_events_on_attributes` | 0 |
| Incorrect Data | Event Attribute Type | `incorrect_event_attribute_datatype` | 0 |
| Incorrect Data | Object | `duplicate_objects_on_ids` | 0 |
| Incorrect Data | Object | `duplicate_objects_on_attributes` | 0 |
| Incorrect Data | Object Attribute Type | `incorrect_attribute_datatype` | 0 |
| Incorrect Data | Object-to-Object | `duplicate_o2o_relations` | 0 |
| Incorrect Data | Object-to-Object | `o2o_self_loop` | 0 |
| Incorrect Data | Event-to-Object | `duplicate_e2o_relations` | 0 |
| | | **Total** | **18,370** |
