# NPL Coding Guidelines Baidu

> **文档信息**
> - 文件: `NPL_Coding_Guidelines_Baidu.pdf`
> - 页数: 14
> - 分类: npl
> - 转换工具: marker
> - 转换时间: 2026-04-25 03:56:44

---


# NPL Coding Guidelines for TD4 Compilation v1.0


# 1 Introduction

This document describes the Network Programming Language coding guidelines.

This document will help NPL application writers create/improve NPL applications. This document covers basic NPL guidelines that the compilers are able to verify. This document also covers application aspects that are not verified by the compilers. These constructs are intended to help the compiler where needed.


# 2 Usage

| 2 Usage | E |  |  | A |  |  |  |
|-----------------------------|--------------------------------------------------|-------------------------------------------------|---------------------------------------------------------------|-------------------------------|--|--|--|
| 2.1 Struct | t as headers |  |  |  |  |  |  |
| High-level object | cts, for example: logical | l_table, logical_register, ar | nd enums can NOT b | pe part of |  |  |  |
|  | uct nesting. However, der invalid use cases. The | epending on the different upy are marked below. | use cases for the stru | ucts there |  |  |  |
| Table 1: struct usage guide |  |  |  |  |  |  |  |
| Struct Usage | bit/bit[n] (can structs have bit) | overlays (can structs have overlays) | 1-level Structs (can structs have one level of structs) | Multi-level Nested Structs |  |  |  |
| Examples |  |  | item is struct.field | item is struct.struct.field |  |  |  |
| as header type | Y | NA | NA | NA |  |  |  |
| as header_group | NA | NA C | Y | NA |  |  |  |
| packet | NA | NA | Y | NA |  |  |  |
| bus | Y | A . A | Y |  |  |  |  |

**Table 2: Referencing struct in Packet**

| Valid packet construction | Is Valid | packet reference description |  |
|----------------------------|----------|--------------------------------|--|
| packet.struct.struct.field | Y | packet.group.header.field |  |
| packet.struct.struct | Y | packet.group.header |  |
| packet.struct.field | N | no group/header |  |
| packet.field | N | packet must have group/header. |  |
| no packet | N |  |  |

## 2.2 Function

Functions with arguments are not supported. Functions must operate on logical bus and packet data.


## 2.3 Overlay Rules

Overlays can be used in multiple constructs and they must follow certain rules which are applicable for all constructs.

- 1. Overlay fields can be defined on Base Fields of type bit/bit[n]
 2. Overlay fields can be defined on Base Fields of type struct. (i.e., fields specified as struct in the bus). However partial overlay of the struct is not allowed.
 3. Overlays can overlap.
 4. Overlay fields can NOT be defined on other overlay fields.
 5. Overlay fields can NOT be of type struct.

## 2.4 Bit-Array Slicing

NPL allows bit-array slicing (ranges) to be specified in the assignments, equations, Special Functions. Bit ranges are allowed on both lvalue and rvalue of equations.

## Example

```npl
a = b[7:4];
if (b[5:3])...
obj_bus.a[5:4] = ing_pkt.ipv4.ecn; // mostly in function, action.
 // single bit access uses range of one bit
a = b[0:0];
```

#### Use Cases

```npl
if (a[5:3]) //supported
a[5:3] = b[5:3]; //support
```

## 2.5 Concatenation Rules

- Concatenations are allowed in assignments rvalue.
 - `a = b<>c;`
- Concatenations are NOT allowed on the lvalue.

```npl
b<>c = a[5:0];
```

- Concatenations are allowed on the same type of fields. i.e., struct<>struct two mpls hdrs = mpls hdr 0<>mpls hdr 1;
- Concatenations are allowed on different types of fields. i.e., struct<>bit new mpls hdr = mpls hdr 0 <> c[3:0];
- Concatenations are allowed on part of the fields. i.e., da[5:0]<>sa[5:3] a[5:0] = b[3:2] <> c[3:0];


# 3 Directives (@NPL_PRAGMA)

To assist compilers, Directives may be specified in the application program. These Directives are used by FE and BE compilers to do placement and other functions. Directives are NOT part of NPL. However, to better aid understanding, directive syntax and usage examples are described.

Directives assist in specifying desired behavior which may have hardware dependencies. Directives assist in BE Mapping.

#### Rules:

- Directives may be specified in the NPL logic file or in a separate file.
- There is no positioning associated with directives.
- Directives must start with a newline.

## 3.1 Format

This is the syntax and keywords used to specify the directives.

```npl
@NPL_PRAGMA(object_name, property_name:property_value);
```

object_name: Refers to the logical component name.

property_name: Can be one of the directives listed in section 3.2.

property_value: Depends on the property_name and contains the physical component related information.

## 3.2 Directives

### 3.2.1 The bus_type Directive

The current bus implementation in hardware has wide buses that span across multiple stages and carry packet and pipeline meta-data. However, there are also buses whose context is localized to a pipeline block. A user can find some of these localized buses by looking for scr in the bus documentation.

The bus type directive is used to map some of the logical buses defined in the NPL to buses which have a localized context in the hardware.

### Format:

```npl
@NPL_PRAGMA(<bus_name>, bus_type: <type>)
```

#### Example:

```npl
bus fsl_scratch_bus s_fsl_scratch_bus;
@NPL_PRAGMA(fsl_scratch_bus, bus_type: temp);
```

Currently, "temp" is the only supported type. "Temp" indicates that the bus is a temporary bus and


only used within the component.

### 3.2.2 The mapping Directive

The mapping directive is used to map a particular table, function, sfc or bus_field to a specific hardware block.

#### 3.2.2.1 Function

The mapping function directive assigns a function to a hardware block.

Some of the hardware blocks to which functions can be assigned are: FSL Blocks, Flex Editor

#### Format:

```npl
@NPL_PRAGMA(<function>, mapping: "<hw_block>")
```

#### Example:

```npl
function vlan_assign_functions()
@NPL_PRAGMA(vlan_assign_functions, mapping: "hw_proc_bl")
```

#### 3.2.2.2 Table

This directive will map a logical table lookup to a hardware tile and physical lookup.

By default the logical table lookup is 0.

The hardware block is deciphered as follows: pipelinestage tiletype tilenumber

#### Format:

```npl
@NPL_PRAGMA(<logical_table_lookup>, mapping: "<hardware block, physical lookup>")
```

## Example 1: Mapping a logical table with a single lookup to a physical table

```npl
@NPL_PRAGMA(ing_system_port_table, mapping: "ifta10_i1t_00, 0")
```

ing_system_port_table is the logical table that needs to be mapped. ifta10 is the pipeline block where the physical table is located.

Note: Knowledge and access to the pipeline diagram will be required to map these tables correctly in order to achieve the desired packet flow.

- i1t is the type of lookup that will be performed. The other options are t4t, e2t, t2t.
- Each Pipeline stage might have multiple index, tcam or exact match tables. The third literal here "00" refers to the table number within that stage that one wants to use for this mapping.

## Example 2: Mapping multiple logical tables to multiple physical tables within the same hardware block

```npl
@NPL_PRAGMA(egr_l3_next_hop_1, mapping: "efta10_i1t_00, 0")
@NPL_PRAGMA(egr_l3_next_hop_2, mapping: "efta10_i1t_01, 0")
@NPL_PRAGMA(egr_l3_oif_1, mapping: "efta10_i1t_02, 0")
@NPL_PRAGMA(egr_dvp, mapping: "efta10_i1t_03, 0")
```


This is a minor variation of Example 1. Multiple single lookup logical tables exist in the application packet flow and their order of processing is relatively sequential. They are mapped to the same hardware block efta10 but different physical tables.

## Example 3: Mapping logical table with two logical lookups to physical resources

```npl
@NPL_PRAGMA(l2_host_narrow_table._LOOKUP0, mapping:"ifta80_e2t_01, 0")
@NPL_PRAGMA(l2_host_narrow_table._LOOKUP1, mapping:"ifta80_e2t_01, 1")
```

In this example, l2_host_narrow_table has two lookups. A e2t tile is utilized and each logical lookup is directly mapped to a physical lookup.

## Example 4: Mapping two logical tables to the same physical resource

```npl
@NPL_PRAGMA(l3_ipv4_unicast_table._LOOKUP0, mapping:"ifta80_e2t_01, 0")
@NPL_PRAGMA(l3_ipv6_unicast_table._LOOKUP0, mapping:"ifta80_e2t_01, 0")
```

In this example, l3_ipv4_unicast_table and l3_ipv6_unicast_table are both mapped to the same physical resource as long as the tables are mutually exclusive. The NPL application developer must take responsibility to guarantee mutual exclusivity in such situations by making sure that the NPL is written in such a way that both tables cannot be looked up for the same packet.

## Example 5: Mapping an unique logical table to an unique lookup in each tile

```npl
@NPL_PRAGMA(protocol_pkt_forward_table, mapping:"ifta60_t4t_00, 1")
@NPL_PRAGMA(pkt_integrity_check_table, mapping:"ifta60_t4t_00, 2")
```

In this example, protocol_pkt_forward_table is mapped to ifta60_t4t_00's first lookup.

pkt_integrity_check_table is mapped to the same tile in the same pipeline stage but to the second lookup.

#### Example 6: A logical table spanning across multiple physical tiles

```npl
@NPL_PRAGMA(protocol_pkt_forward_table, mapping:"ifta60_t4t_00, 0")
@NPL_PRAGMA(protocol_pkt_forward_table, mapping:"ifta60_t4t_00, 1")
```

A logical table might need to occupy two physical tables because it requires a wider scale. This example highlights one such case where one logical table spans across two physical tiles providing increased depth.

### Example 7: Dynamic Table

```npl
@NPL_PRAGMA(ifp, mapping:"ifta100_t4t_00, 0")
@NPL_PRAGMA(ifp, mapping:"ifta100_t4t_00, 1")
@NPL_PRAGMA(ifp, mapping:"ifta100_t4t_00, 2")
@NPL_PRAGMA(ifp, mapping:"ifta100_t4t_00, 3")

@NPL_PRAGMA(ifp, mapping:"ifta100_t4t_01, 0")
@NPL_PRAGMA(ifp, mapping:"ifta100_t4t_01, 1")
@NPL_PRAGMA(ifp, mapping:"ifta100_t4t_01, 2")
@NPL_PRAGMA(ifp, mapping:"ifta100_t4t_01, 3")
```


```npl
@NPL_PRAGMA(ifp, mapping:"ifta100_t4t_02, 0")
@NPL_PRAGMA(ifp, mapping:"ifta100_t4t_02, 1")
@NPL_PRAGMA(ifp, mapping:"ifta100_t4t_02, 2")
@NPL_PRAGMA(ifp, mapping:"ifta100_t4t_02, 3")
```

A dynamic table is one where the keys and actions of the table are both flexible. The match and action are configurable on a per packet flow basis using SDKLT.

#### 3.2.2.2.1 Tile Mode

Tile Mode allows users to partition the table resources in the most effective manner for their deployment. It allows for tables associated with a feature to scale higher/lower depending on use case. Broadcom has defined Tile Modes for most of these tables allowing for effective sharing of resources. Any new tile mode addition will be the users responsibility to manage and bring it up through SDKLT tooling.

#### Examples:

```npl
@NPL_PRAGMA(my_station_table, mapping:"ifta40_t4t_00, 0, Tile_Mode:0")
@NPL_PRAGMA(my_station_table, mapping:"ifta40_t4t_00, 1, Tile_Mode:0")
@NPL_PRAGMA(13_ipv4_tunnel_table, mapping:"ifta40_t4t_00, 2, Tile_Mode:0")
@NPL_PRAGMA(13_ipv4_tunnel_table, mapping:"ifta40_t4t_00, 3, Tile_Mode:0")
@NPL_PRAGMA(13_ipv6_tunnel_table, mapping:"ifta40_t4t_00, 2, Tile_Mode:0")
@NPL_PRAGMA(13_ipv6_tunnel_table, mapping:"ifta40_t4t_00, 3, Tile_Mode:0")
```

#### 3.2.2.2.2 Access Mode

Access Modes apply only to table mappings. There are some tiles in the device which can be made deeper by changing the access mode. Another method to get deeper tables is to reduce the policy width. The compiler will recognize the reduced policy width and scale the table accordingly.

By default Compiler allocates Tile_Mode_0 with ACC_MODE 0 to get 32k next hops. In Tile_Mode_1- NPL writer/user tells Compiler to change ACC_MODE 1 to get 48k next hops.

#### Examples:

```npl
@NPL_PRAGMA(ing_13_next_hop_1_table, mapping:"ifta130_i1t_00, 0, Tile_Mode:0")
@NPL_PRAGMA(ing_13_next_hop_1_table, mapping:"ifta130_i1t_00, 0, Tile_Mode:1, ACC_MODE:1")
3.2.2.2.3 Tag
```

This directive applies only to tables. This is specifically used for alpm tables and is an assist to the SDKLT tooling to let it know that it needs to chain output Databases to produce expanded policy data. This is also used by the compiler to place ALPM Tables.

#### Examples:

```npl
@NPL_PRAGMA(l3_ipv4_alpm_table._LOOKUP0, mapping:"ifta80_t2t_00, 0, Tile_Mode:2, TAG:ALPM_DIP")
@NPL_PRAGMA(l3_ipv4_alpm_table._LOOKUP0, mapping:"ifta80_t2t_00, 1, Tile_Mode:2, TAG:ALPM_DIP")
@NPL_PRAGMA(l3_ipv6_alpm_table._LOOKUP0, mapping:"ifta80_t2t_00, 0, Tile_Mode:2, TAG:ALPM_DIP")
@NPL_PRAGMA(l3_ipv6_alpm_table._LOOKUP0, mapping:"ifta80_t2t_00, 1, Tile_Mode:2, TAG:ALPM_DIP")
```


#### 3.2.2.3 Bus Field

This directive will map a bus field to a hardware functionality.

#### Format:

```npl
@NPL_PRAGMA(<bus.field>, mapping: "<type [, Option]>")
```

"Type" can be one of the following: "fixed", "alpm", "alpm_key_mux",
"flex_editor_bitmap_mode", "mirror_encap_profile_index", "parser_internal"

## Examples:

```npl
@NPL_PRAGMA(egr_edit_bitmap_bus.l2_tags_hg3_insert_bitmap, mapping: "flex_editor_bitmap_mode")
@NPL_PRAGMA(ing_hw_bus.parser_vhlen_0, mapping: "fixed, VHLEN")
@NPL_PRAGMA(ing_obj_bus.vrf, mapping: "alpm, VRF")
@NPL_PRAGMA(pkt_fwd_field_bus.ip_hdr_dip, mapping: "alpm_key_mux, DIP")
```

### 3.2.3 The allow_sbr Directive

Allow strength based resolution on the specified method.

This option is only supported for dynamic tables.

Note: This may not be needed in X11C.

### Format:

```npl
@NPL_PRAGMA(<dynamic_table.method>, allow_sbr)
```

#### Example:

```npl
@NPL_PRAGMA(efp.sbr_template, allow_sbr)
```

### 3.2.4 The loopback Directive

Identify header as a loopback. The loopback pragma is used by the compiler to make some width checks on the loopback header. Loopback Header needs to be only 128b.

#### Format:

```npl
@NPL_PRAGMA(<header>, loopback: "true")
```

```npl
@NPL_PRAGMA(egress_pkt.sys_hdr.loopback, loopback: "true")
```

### 3.2.5 The mirror_transport Directive

Identify header as a mirror transport. Max Mirror encap is 80B. Designating a header as mirror transport header used to check size.

#### Format:

```npl
@NPL_PRAGMA(<header>, mirror_transport: "true")
```


## Example:

```npl
@NPL_PRAGMA(egress_pkt.sys_hdr.mirror_transport, mirror_transport: "true")
```

### 3.2.6 The internal_header_group Directive

Identify header as internal header. Internal Header groups are expected to be at the head of the header shared line stack and explicit definition of a header as internal header group helps the compiler with that particular check.

#### Format:

```npl
@NPL_PRAGMA(<header>, internal_header_group: "true")
```

#### Example:

```npl
@NPL_PRAGMA(ingress_pkt.sys_hdr, internal_header_group: "true")
```

### 3.2.7 The key_select_mapping Directive

Identify function as key select operation.

#### Format:

```npl
@NPL_PRAGMA(<function>, key_select_mapping: <option>)
```

#### Example:

```npl
@NPL_PRAGMA(qos_remarking_select_index, key_select_mapping: qos_remarking)
```

### 3.2.8 The force_new_hme Directive

This directive forces the compiler to start parsing a header in a new header match engine. Enforcing such parsing might end up in sub-optimal parser tree optimization.

#### Format:

```npl
@NPL_PRAGMA(<parser_node>, force_new_hme: <option>)
```

#### Example:

```npl
@NPL_PRAGMA(unknown_15, force_new_hme: "true")
```

### 3.2.9 The remove_from_match_id Directive

This directive was needed to overcome a hardware limitation in X9 and X11 caused by an explosion of headers matched in certain parsing zones. This should no longer be needed in X11C as the hardware has been evolved to take care of such an increase.

#### Format:

```npl
@NPL_PRAGMA(<header>, remove_from_match_id: <option>)
```

#### Example:


### 3.2.10 The varbit_parse_unbounded Directive

If a header is set as varbit_parse_unbounded, the parser cannot continue parsing beyond this header.

#### Format:

```npl
@NPL_PRAGMA(<header>, varbit_parse_unbounded: "true")
```

