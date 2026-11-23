# Rule-based detection: `enron-all-mails`

*Log:* `data/real-world/enron-all-mails.sqlite` (1053 MB) — *date:* 2026-11-22 17:16 — *detection runtime:* 15.1 s

Rows reported by each rule detector on the log as published (no ground truth, no LLM, no repair).

## Log

| Table | Rows |
|---|---:|
| event | 517,891 |
| object | 316,848 |
| event_object | 1,765,575 |
| object_object | 0 |
| event types | 9 |
| object types | 2 |

## Findings

| Row | Cell | Issue | Count |
|---|---|---|---:|
| Missing Data | Event | `missing_event` | 0 |
| Missing Data | Event Type | `missing_event_type` | 0 |
| Missing Data | Event Time | `missing_event_timestamp` | 0 |
| Missing Data | Event Attribute Value | `missing_event_attribute_value` | 21,230 |
| Missing Data | Object | `missing_object` | 0 |
| Missing Data | Object Type | `missing_object_type` | 0 |
| Missing Data | Object Attribute Value | `missing_attribute_value` | 0 |
| Missing Data | Object-to-Object | `dangling_o2o_relationship` | 0 |
| Missing Data | Event-to-Object | `dangling_e2o_relationship` | 0 |
| Incorrect Data | Event | `duplicate_events_on_ids` | 0 |
| Incorrect Data | Event | `duplicate_events_on_attributes` | 131,694 |
| Incorrect Data | Event Attribute Type | `incorrect_event_attribute_datatype` | 0 |
| Incorrect Data | Object | `duplicate_objects_on_ids` | 0 |
| Incorrect Data | Object | `duplicate_objects_on_attributes` | 0 |
| Incorrect Data | Object Attribute Type | `incorrect_attribute_datatype` | 0 |
| Incorrect Data | Object-to-Object | `duplicate_o2o_relations` | 0 |
| Incorrect Data | Object-to-Object | `o2o_self_loop` | 0 |
| Incorrect Data | Event-to-Object | `duplicate_e2o_relations` | 0 |
| | | **Total** | **152,924** |
