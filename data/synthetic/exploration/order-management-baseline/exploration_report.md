# OCEL Exploration Report

*Database:* `data/synthetic/order-management-baseline.sqlite` — *model:* `qwen2.5:7b`

## 1. Domain

**Process:** An online retail and order fulfillment process (confidence: high)

- Multiple 'pick item' events for each product
- Customer placing 'place order' and paying through 'pay order'
- Orders are fulfilled with 'send package' and 'package delivered'
- 'item out of stock' triggers reordering

## 2. Object Types

### orders  (`object_Orders`)

- **Represents:** Order events in an online retail process
- **IDs:** ocel_id contains unique identifiers for each order event, prefixed with 'o-' and followed by a seven-digit number. — dominant pattern `o-######` (100%) (usable as entity name: **no**)

| attribute | null rate | meaning | value source | domain knowledge | NULL by design | known values | repair hint |
|---|---|---|---|---|---|---|---|
| `ocel_time` | 0 | Timestamp of the order event | events | yes | no | — | Use the timestamp from the event log. |
| `ocel_changed_field` | 1.0 | Field that was changed during the order event (always null in this dataset) | unknown | no | yes | — | This field is always null, indicating no delta encoding. |

### items  (`object_Items`)

- **Represents:** Items in an online retail and order fulfillment process
- **IDs:** ocel_id values contain unique identifiers for each item, which can be used to track individual objects throughout the process. — dominant pattern `i-######` (100%) (usable as entity name: **no**)

| attribute | null rate | meaning | value source | domain knowledge | NULL by design | known values | repair hint |
|---|---|---|---|---|---|---|---|
| `ocel_changed_field` | 1.0 | Identifies which field (attribute) was changed in the event, but is null here as per evidence | unknown | no | yes | — | Not applicable; this column is used for delta encoding and not populated here. |
| `weight` | 0 | Weight of the item in kilograms | object_id | yes | no | — | Measure or look up the weight based on the object ID. |
| `price` | 0 | Price of the item in dollars | object_id | yes | no | — | Look up the price based on the object ID or known pricing data. |

### packages  (`object_Packages`)

- **Represents:** Packages in an online retail and order fulfillment process
- **IDs:** ocel_id values are unique identifiers for each package, prefixed with 'p-' and containing numeric IDs — dominant pattern `p-######` (100%) (usable as entity name: **no**)

| attribute | null rate | meaning | value source | domain knowledge | NULL by design | known values | repair hint |
|---|---|---|---|---|---|---|---|
| `ocel_time` | 0 | Timestamp of the event involving the package | events | yes | no | — | Use the timestamp from the corresponding event |
| `ocel_changed_field` | 1.0 | Field that changed in the package record (always null here, indicating delta encoding) | unknown | no | yes | — | Not applicable; always null as per evidence |

### customers  (`object_Customers`)

- **Represents:** Timestamps and changed fields for customer objects in an event log
- **IDs:** ocel_id contains unique identifiers for each customer object, likely their entity names (usable as entity name: **yes**)

| attribute | null rate | meaning | value source | domain knowledge | NULL by design | known values | repair hint |
|---|---|---|---|---|---|---|---|
| `ocel_time` | 0 | Timestamp when the event involving this customer occurred | events | no | no | 2023-04-03 01:00:00 | Use the timestamp from the corresponding event |
| `ocel_changed_field` | 1.0 | Field name that was changed in the customer object during the event, if any; otherwise NULL | unknown | no | yes | — | Indicates a change but no specific field is provided, so leave as NULL |

### products  (`object_Products`)

- **Represents:** Product instances in an online retail and order fulfillment process
- **IDs:** ocel_id contains unique identifiers for each product instance, which can be used to track changes over time (usable as entity name: **yes**)

| attribute | null rate | meaning | value source | domain knowledge | NULL by design | known values | repair hint |
|---|---|---|---|---|---|---|---|
| `ocel_time` | 0 | Timestamp of the event involving the product | events | yes | no | 2023-04-03 01:00:00, 2023-06-02 11:39:36, 2023-08-01 14:17:59, 2023-09-30 16:24:43, 2023-12-01 10:01:30, 2024-01-30 11:45:23 | Use the closest timestamp from the events table |
| `ocel_changed_field` | 0.167 | Attribute of the product that was changed in this event | derived_from_other_attributes | no | yes | price | Indicates which attribute was updated, if any |
| `weight` | 0.833 | Weight of the product in kilograms | unknown | no | yes | — | Can be estimated based on known products or categories |
| `price` | 0 | Price of the product in dollars | unknown | no | yes | — | Can be estimated based on known products or categories |

### employees  (`object_Employees`)

- **Represents:** The roles and timestamps associated with employees in an organizational process
- **IDs:** ocel_id contains unique identifiers for individual employees, which can be used to track their involvement over time (usable as entity name: **yes**)

| attribute | null rate | meaning | value source | domain knowledge | NULL by design | known values | repair hint |
|---|---|---|---|---|---|---|---|
| `ocel_time` | 0 | Timestamp of the event involving the employee | events | yes | no | 2023-04-03 01:00:00 | Use the earliest or latest timestamp if genuinely missing |
| `ocel_changed_field` | 1.0 | The attribute that changed during the event, but is not applicable as it's always null in this case | unknown | no | yes | — | Leave NULL as it indicates no change |
| `role` | 0 | The role of the employee within the organization at the time of the event | domain_knowledge | yes | no | Sales, Shipment, Warehousing | Assign a default role if genuinely missing |

## 3. Event Types

**Typical process order (from the data):** place order → item out of stock → confirm order → pick item → reorder item → create package → send package → failed delivery → payment reminder → pay order → package delivered

### pay order  (`event_PayOrder`)

- **Represents:** The payment of an order in an online retail process.
- **When:** Occurs after 'confirm order' and before 'package delivered' in the typical process flow.
- **IDs:** ocel_id values are opaque tokens that uniquely identify each pay order event within the log. — dominant pattern `pay_o-######` (100%)

| attribute | meaning | equals linked object | repair hint |
|---|---|---|---|
| `ocel_time` | The timestamp of when the payment was made. | — | Use the actual time recorded for each event. |
| `amount` | The monetary value of the order being paid. | — | Fill with the amount reported in the 'amount' attribute of the corresponding pay order event. |

### package delivered  (`event_PackageDelivered`)

- **Represents:** The delivery of a package by the shipper to the customer.
- **When:** This event type occurs after 'send package' and before any failure events like 'failed delivery', indicating successful completion of the delivery process.
- **IDs:** Ocel_id values are opaque tokens that uniquely identify each delivery event, useful for tracking individual deliveries. — dominant pattern `deliver_p-######` (100%)

| attribute | meaning | equals linked object | repair hint |
|---|---|---|---|
| `ocel_time` | The timestamp when the package was delivered. | — | Use the actual time recorded in the event log. |
| `resource` | The shipper responsible for delivering the package. | `shipper` (100%) | Assign a known shipper based on context or domain knowledge. |

### place order  (`event_PlaceOrder`)

- **Represents:** The placement of an order for items by customers in the online retail process.
- **When:** 'place order' typically occurs at the beginning of the typical process flow before any item out of stock or confirm order events.
- **IDs:** OCEL IDs are opaque tokens that uniquely identify each 'place order' event, serving as a reference within the log. — dominant pattern `place_o-######` (100%)

| attribute | meaning | equals linked object | repair hint |
|---|---|---|---|
| `ocel_time` | The timestamp when the order was placed. | — | Use the exact time recorded in the event log. |
| `item_count` | The number of items included in the order. | — | Estimate based on known average item counts or use a default value if no data is available. |

### create package  (`event_CreatePackage`)

- **Represents:** The creation of a package for an order, including the resource (employee) who created it.
- **When:** 'create package' occurs after 'pick item' and before 'send package' in the typical order of events.
- **IDs:** OCEL IDs are opaque tokens that uniquely identify each 'create package' event; they can be used to track individual events within the process. — dominant pattern `create_p-######` (100%)

| attribute | meaning | equals linked object | repair hint |
|---|---|---|---|
| `ocel_time` | The timestamp when the package was created. | — | Use the actual time recorded during the event. |
| `resource` | The employee who created the package. | `packer` (100%) | Identify and link to the correct employee object based on their name. |

### confirm order  (`event_ConfirmOrder`)

- **Represents:** The timestamp and sales person of the confirmation event.
- **When:** This event typically occurs after 'place order' and before 'pick item' in the process lifecycle.
- **IDs:** Ocel_id values are opaque tokens that uniquely identify each confirm order event. — dominant pattern `confirm_o-######` (100%)

| attribute | meaning | equals linked object | repair hint |
|---|---|---|---|
| `ocel_time` | The timestamp of the confirmation event | — | Use the recorded timestamp from the event log. |
| `resource` | The sales person who confirmed the order | `sales person` (100%) | Identify the corresponding sales person using the object_id. |

### item out of stock  (`event_ItemOutOfStock`)

- **Represents:** The timestamp and responsible employee for an item becoming out of stock.
- **When:** 'Item out of stock' events typically occur between placing an order and confirming it, suggesting they happen when inventory levels drop below expected levels during the ordering or fulfillment phase.
- **IDs:** OCEL IDs are opaque tokens that uniquely identify each 'item out of stock' event, allowing tracking of specific instances within the process. — dominant pattern `out_of_stock_i-######` (100%)

| attribute | meaning | equals linked object | repair hint |
|---|---|---|---|
| `ocel_time` | The timestamp when the item became out of stock. | — | Use the actual time recorded in the event log. |
| `resource` | The employee who noticed or reported that an item is out of stock. | `employee` (100%) | Assign the employee responsible for noticing the issue, typically a staff member involved in inventory management. |

### pick item  (`event_PickItem`)

- **Represents:** The time and person performing the pick item event in an online retail process.
- **When:** This event type occurs after 'confirm order' and before 'reorder item' in the typical process flow, indicating it happens when items are being picked from stock to fulfill a confirmed order.
- **IDs:** ocel_id values are unique identifiers for each pick item event, likely representing the specific instance or transaction of picking an item. — dominant pattern `pick_i-######` (100%)

| attribute | meaning | equals linked object | repair hint |
|---|---|---|---|
| `ocel_time` | The timestamp of the pick item event. | — | Use the exact time recorded in the log. |
| `resource` | The employee who performed the pick item action. | `employee` (100%) | Assign based on the employee attribute linked to this event. |

### send package  (`event_SendPackage`)

- **Represents:** The event type `send package` marks the point in time when a package is dispatched from the shipper.
- **When:** This event happens after the creation of the package and before potential delivery attempts or failed deliveries.
- **IDs:** Ocel_id values are opaque tokens that uniquely identify each send package event, which can be used to track individual packages throughout their process lifecycle. — dominant pattern `send_p-######` (100%)

| attribute | meaning | equals linked object | repair hint |
|---|---|---|---|
| `ocel_time` | The timestamp when the package was sent out for delivery. | — | Use the actual time recorded in the event log. |
| `resource` | The identifier of the shipper responsible for sending the package. | `shipper` (100%) | Assign the id of the corresponding shipper object. |

### payment reminder  (`event_PaymentReminder`)

- **Represents:** The timestamp when the payment reminder event occurs.
- **When:** This event type typically comes after 'payment reminder' in the process order, suggesting it may be related to failed delivery or other issues needing resolution before final payment.
- **IDs:** Ocel_id values are opaque tokens that uniquely identify each payment reminder event within the log. — dominant pattern `reminder_o-######_###` (67%)

| attribute | meaning | equals linked object | repair hint |
|---|---|---|---|
| `ocel_time` | The timestamp of when the payment reminder was issued. | — | A repair agent should fill a genuinely missing value with the actual time recorded in the log. |

### reorder item  (`event_ReorderItem`)

- **Represents:** The time and employee when an item is reordered in the process.
- **When:** Reorder events typically occur after 'item out of stock' and before 'confirm order'.
- **IDs:** OCEL IDs are opaque tokens that uniquely identify each reorder event, containing no explicit information about the content of the event but ensuring its uniqueness. — dominant pattern `reorder_i-######` (100%)

| attribute | meaning | equals linked object | repair hint |
|---|---|---|---|
| `ocel_time` | The timestamp when the reorder event occurs. | — | Use the actual time recorded in the event log. |
| `resource` | The employee who initiated the reorder process. | `employee` (100%) | Identify the employee from the corresponding 'employee' qualifier or attribute. |

### failed delivery  (`event_FailedDelivery`)

- **Represents:** A failed delivery event where the package was not successfully delivered to the customer.
- **When:** This event type occurs after the package has been shipped but before it is delivered. Typically, 'create package' and 'send package' come right before it, while 'package delivered' or 'payment reminder' follow if applicable.
- **IDs:** The ocel_id values are opaque tokens that uniquely identify each failed delivery event, containing no meaningful information beyond their uniqueness. — dominant pattern `fail_p-######_###` (67%)

| attribute | meaning | equals linked object | repair hint |
|---|---|---|---|
| `ocel_time` | The timestamp when the failed delivery event occurred. | — | Use the recorded time from the event log. |
| `resource` | The entity responsible for delivering the package, identified by their name. | `shipper` (100%) | Assign a known shipper based on the resource attribute's value. |

## 4. Qualifier Semantics

- `item`: _?_
- `product`: _?_
- `employee`: _?_
- `order`: _?_
- `customer`: _?_
- `shipper`: _?_
- `shipped package`: _?_
- `sales person`: _?_
- `packer`: _?_
- `forwarder`: _?_
- `creates`: _?_
- `is a`: _?_
- `contains`: _?_
- `comprises`: _?_
- `places`: _?_
- `shipped by`: _?_
- `packed by`: _?_
- `forwarded by`: _?_
- `secondarySalesRep`: _?_
- `primarySalesRep`: _?_
