# XFlow logical-table control-plane changes

Use this reference when an NPL `logical_table` gains a field, selector, action, or new CRUD operation. It is a project integration contract, not an NPL language rule.

## Required impact walk

Trace the field from the NPL logical-table declaration and generated metadata through every active control-plane layer:

1. schema/table name and field symbol;
2. generated SDKLT metadata and the target chip-variant ID header;
3. RPC/gRPC field encoding and table-ID lookup;
4. agent client read/write and response decoding (historical layout: `inspect/client/<table>.go`);
5. service wrapper, HTTP route/body, and CLI parsing/help (historical layout: `service/<table>.go`, `route.go`, `tools/xflow-client/main.go`);
6. independent product fixture, expected result, and read-back assertion.

For each layer record field ID source, width, signedness, enum/bool representation, key/data role, selector branch, default, and read/write direction. A field that is present in NPL but absent from one layer is incomplete even when compilation succeeds.

Resolve those illustrative paths through the current repository profile. A new CLI command without its agent route produces an HTTP failure; an RPC field without matching agent decoding can read back zero or fail a type assertion. Trace actual callers instead of assuming every requirement changes all layers.

## Metadata rules

- Resolve FieldId from generated metadata for the configured chip and SDKLT revision. Common and chip-variant headers are separate sources; if the common header has no symbol, search the configured variant output. Never infer IDs from `lt list` order or an old table dump.
- The historical SDKLT symbol shape is `<TABLE>t_<FIELD>f`; common IDs appeared in `bcmltd_common_id.h`, while project table IDs appeared in variant `*_ltd_id.h` outputs. Confirm those locations from the pinned SDK metadata; request the generated file as remote evidence if it is not part of the checkout.
- Keep width and conversion consistent end to end. Decode numeric, boolean, and symbol oneof values according to generated metadata; do not cast every response to one wire type.
- The observed XFlow read path used `DataField_Stream` for numeric decimal bytes, `DataField_BoolVal` for boolean values, and `DataField_StrVal` for symbols; the write shape differed. Check the current protobuf/server codec before applying this mapping. For a contract accepting hexadecimal input, support it explicitly and range-check against the actual bit width rather than truncating in a narrower Go/JSON field.
- For selector fields, write only the selected branch and test both branches plus a conflicting input. For sparse field IDs, enumerate the actual field list when reading; a mapped-field count is not an ID range.
- Resolve the table ID from the current forwarding-pipeline metadata rather than hard-coding an observed numeric ID.

## Acceptance evidence

Require an insert/update/read-back for each new field, the selector and invalid combinations where applicable, and a cross-check against the device/API boundary supplied by the project profile. Keep implementation precedent (`observed`) separate from current generated metadata (`confirmed`).

Example: widening a field from 8 to 16 bits requires a value above 255 to survive CLI → HTTP body → agent/RPC write → read-back. Test the upper valid value and one invalid overflow as well. If a selector chooses another field, assert the chosen branch and reject or handle a contradictory input according to the approved API contract. No historical numeric FieldId is needed for these assertions.
