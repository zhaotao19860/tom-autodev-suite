# NPL Compilation Error Fixup Examples Baidu

> **文档信息**
> - 文件: `NPL_Compilation_Error_Fixup_Examples_Baidu.pdf`
> - 页数: 16
> - 分类: npl
> - 转换工具: marker
> - 转换时间: 2026-04-25 04:13:31

---

# 1 Common

## 1.1 Parser/MPB/Edit Problem: Egress Parser and Edit has 20B offset in header

- NCSP: NCSP_INTERNAL_M_6.5.30.8
- Problem: Extracted fields by egress parser has 20B offset, let's say, byte 80-99 should be extracted, but actually 60-79 was extracted. Also flex edit has deleted header or added header at the position that is off by 20B.
- Cause: VHLEN was not connected from parser to EP correctly.
- The NPL code to correct the issue: after adding following highlighted NPL code, the problem is fixed.

```npl
ing_hw_bus.parser_vhlen_0 = 0;
ing_mpb_flex_bus.parser_vhlen_0 = ing_hw_bus.parser_vhlen_0;
```

## 1.2 Parser/FSL/Table Problem: Bus Mapping of component Bus failed

- NCSP: NCSP_INTERNAL_M_6.5.30.8
- Problem: shown below

```npl
INFO: xfc.bus.bus_be: Placed field: parser1_field_bus.sharp_tree_id[767:760]
 Placed index/Max index: 140/339<8>
INFO: xfc.bus.bus_be: logical consumer producer TAP Points
 PROD: PARSER.in_nwk_comp.OUT
 CONS: sharp_tree_id_table.IN
INFO: xfc.bus.bus_be: physical consumer producer TAP Points
 PROD: iparser1.OUT
 CONS: ifta50_i1t_01.IN
CRITICAL: main: (critical): Bus Mapping of component Bus failed with exception <class 'KeyError'> 7
```

- NPL code to cause the problem

```npl
Parser:
parser_node in_nwk_comp {
 extract_fields(ingress_pkt.outer_l3_l4_hdr.in_nwk_comp);

parser1_field_bus.sharp_tree_id = ingress_pkt.outer_l3_l4_hdr.in_nwk_comp.tree_id;
parser1_field_bus.sharp_src_rank = ingress_pkt.outer_l3_l4_hdr.in_nwk_comp.src_rank;

parser1_field_bus.sharp_sequence = ingress_pkt.outer_l3_l4_hdr.in_nwk_comp.sequence[7:0];
// sharp_reserve is used to carry sharp_metadata in the prototyping
// sharp_reserve[15:12]: sharp_type
// sharp_reserve[11:8]: sharp_pass_id
parser1_field_bus.sharp_reserve = ingress_pkt.outer_l3_l4_hdr.in_nwk_comp.reserve[15:8];

next_node sharp_data_part0;
}
Table:
logical_table sharp_tree_id_table {
 table_type : index;
 minsize : 16384;
```

```npl
maxsize : 16384;
keys {
 bit[FIELD_16_WD] tree_id;
}
fields {
 bit[FIELD_5_WD] ctr_idx_high;
}
key_construct() {
 if (_LOOKUP0 == 1) {
 tree_id = parser1_field_bus.sharp_tree_id;
 }
}
fields_assign() {
 if (_LOOKUP0 == 1) {
 if (_VALID == 1) {
 ing_obj_bus.sharp_pool_id[12:8] = ctr_idx_high;
 }
 }
}
```

- NPL Code to Solve the Problem

```npl
Parser:
parser_node in_nwk_comp {
 extract_fields(ingress_pkt.outer_l3_l4_hdr.in_nwk_comp);
 parser1_field_bus.sharp_tree_id = ingress_pkt.outer_l3_l4_hdr.in_nwk_comp.tree_id;
 parser1_field_bus.sharp_src_rank = ingress_pkt.outer_l3_l4_hdr.in_nwk_comp.src_rank;
 parser1_field_bus.sharp_sequence = ingress_pkt.outer_l3_l4_hdr.in_nwk_comp.sequence[7:0];
 // sharp_reserve is used to carry sharp_metadata in the prototyping
 // sharp_reserve[15:12]: sharp_type
 // sharp_reserve[11:8]: sharp_pass_id
 parser1_field_bus.sharp_reserve = ingress_pkt.outer_l3_l4_hdr.in_nwk_comp.reserve[15:8];
 next_node sharp_data_part0;
}

Function:
 ing_obj_bus.sharp_tree_id = parser1_field_bus.sharp_tree_id;

logical_table sharp_tree_id_table {
 table_type : index;
 minsize : 16384;
 maxsize : 16384;
 keys {
 bit[FIELD_16_WD] tree_id;
 }
 fields {
 bit[FIELD_5_WD] ctr_idx_high;
 }
 key_construct() {
 if (_LOOKUP0 == 1) {
 tree_id = ing_obj_bus.sharp_tree_id;
 }
 }
 fields_assign() {
 if (_LOOKUP0 == 1) {
 if (_VALID == 1) {
 ing_obj_bus.sharp_pool_id[12:8] = ctr_idx_high;
 }
 }
 }
}
```

## 1.3 Parser/Edit Problem: Unexpected header field order after packet edit

- NCSP: NCSP INTERNAL M 6.5.30.8
- Problem: At 2<sup>nd</sup>-pass response processing, added new header was not in contiguous area but interleaved with added headers in 1st pass.
- Cause: In response processing, 96B payload data is expected not to parse, but generated flexcode.yml is incorrect such that 96B packet header was parsed unexpectedly at 2<sup>nd</sup> pass of response processing.
- Solution: Pick up BE compiler update at parser_be.py and flexcode.yml presents following update with compiler update:
   - IPARSER1_HME_STAGE1_TCAM[0].DATA.NEXT_HDR_TYPE_OFFSET is changed from 0 to 56 such that sharp type in INC.reserve field can be correctly extracted as next stage HDR TYPE key field.
   - IPARSER1_HME_STAGE2_TCAM[0].MASK.HDR_TYPE is changed from 0 to 0xe000 such that correct mask as specified in NPL is being used to match sharp type

# 2 Flex Edit

## 2.1 Add_header: added header present in unexpected position

- NCSP: NCSP_INTERNAL_M_6.5.30.8
- Problem: In NPL code, following code is written to add headers. But in the test, the added header is present in unexpected position.

```npl
egress_pkt.l5_l6_hdr.sharp_data_part0.data0 = egr_obj_bus.sharp_response_part_6_7_data0;
egress_pkt.l5_l6_hdr.sharp_data_part0.data1 = egr_obj_bus.sharp_response_part_6_7_data1;
egress_pkt.l5_l6_hdr.sharp_data_part0.data2 = egr_obj_bus.sharp_response_part_6_7_data2;
egress_pkt.l5_l6_hdr.sharp_data_part0.data3 = egr_obj_bus.sharp_response_part_6_7_data3;
add_header(egress_pkt.l5_l6_hdr.sharp_data_part0);

egress_pkt.l5_l6_hdr.sharp_data_part1.data0 = egr_obj_bus.sharp_response_part_2_8_data0;
egress_pkt.l5_l6_hdr.sharp_data_part1.data1 = egr_obj_bus.sharp_response_part_2_8_data1;
egress_pkt.l5_l6_hdr.sharp_data_part1.data2 = egr_obj_bus.sharp_response_part_2_8_data2;
egress_pkt.l5_l6_hdr.sharp_data_part1.data3 = egr_obj_bus.sharp_response_part_2_8_data3;
add_header(egress_pkt.l5_l6_hdr.sharp_data_part1);
```

- Cause: In parser code, the packet in processing have been parsed to generate the header. So adding the existing header without first deletion will have the problem.
- Solution: Do not parser the header to add. First deletion then addition will also cause some fields to be removed unexpectedly.

## 2.2 Flex Edit delete header 96B miss last 16B

- NCSP: NCSP_INTERNAL_M_6.5.30.8
- Problem: Following NPL code is used to delete 96B headers, but actually only 80B was deleted:

```npl
delete_header(egress_pkt.l5_l6_hdr. sharp_data_part0);
delete_header(egress_pkt.l5_l6_hdr. sharp_data_part1);
delete_header(egress_pkt.l5_l6_hdr. sharp_data_part2);
delete_header(egress_pkt.l5_l6_hdr. sharp_data_part3);
delete_header(egress_pkt.l5_l6_hdr. sharp_data_part4);
delete_header(egress_pkt.l5_l6_hdr. sharp_data_part5);
```

- The code to correct the issue: merge the delete headers as follows, and 96B headers can be deleted. NPL compiler team will add check to avoid the issue.

delete_header(egress_pkt.I5 I6 hdr)

## 2.3 Problem: Editor Control Exception

- NCSP: 1.3.16rc1
- Problem description: following error was observed in frontend NPL compilation.

```npl
INFO: xfc.parser.parser_be: Placement Done
INFO: main: Executing Constraints - Editor Control
INFO: xfc.editor_ctrl.editor_ctrl: Creating Rule to Equation map
CRITICAL: main: (critical): Constraints of component Editor Control failed with exception <class 'TypeError'>
object of type 'NoneType' has no len()
make lint
```

- Cause: In the BE compiler function _process_mirror_signal()@cm/xfc/editor_ctrl/editor_ctrl.py, self.mr_signal might be None, and len(self.mr_signal) will report exception if self.mr_signal is None

```npl
def _process_mirror_signal(self, rule):
 if not rule['VALID']:
 return rule
 _mrs = list()
 if len(self.mr_signal) > 0:
 _normal = rule['CONDITION']['NORMAL']
```

- Solution: modify the code to perform len operation only when self.mr signal is not None

```npl
def _process_mirror_signal(self, rule):
 if not rule['VALID']:
 return rule
 _mrs = list()
```

```npl
if self.mr_signal is not None and len(self.mr_signal) > 0:
 _normal = rule['CONDITION']['NORMAL']
```

- Debug Method: add "self.log.info('\*\*\*\*debug')" to find out the location where the error is reported

# 3 MPB

## 3.1 MPB Problem: ing_mpb_flex_bus.drop_code is unassigned

- NCSP: NCSP_INTERNAL_M_6.5.30.8
- Problem: following error was reported during NPL FE compilation

```npl
ERROR: <mmu_process.npl>:14: 'mmu.usage_mode_create', 'ing_mpb_flex_bus' has un assigned fields,
 bus-field: ing_mpb_flex_bus.drop_code is unassigned.

Compilation Failed...
```

- The code to cause the error

```npl
Mbp_encode():
 ing_mpb_flex_bus.cpu_opcode = ipost_scratch_bus.event_trace_vector;
 //ing_mpb_flex_bus.drop_code = ipost_scratch_bus.drop_code;

struct mpb_flex_t {
 ...
 /* TARGET_COMPILE, consumer_check: "disable" */
 bit[FIELD_16_WD]
```

- Cause: drop_code was declared in mpb_flex_t, but was not assigned with values
- The code to correct the error:

```npl
struct mpb_flex_t {
...
///* TARGET_COMPILE, consumer_check: "disable" */
// bit[FIELD_16_WD] drop_code;
}
```

## 3.2 Problem: MPB Unexpected container size dist, for ing_obj_bus.vni

- Background

NCSP 1 3 14rc0?

Problem Description

Following error was reported in BE compilation:

```npl
5324 ERROR: xfc.mpbencode.mpb_be: [MPB-02-1009]:
5325 Unexpected container size dist, for ing_obj_bus.vni
5326 ERROR: xfc.mpbencode.mpb_be: [MPB-02-1009]:
5327 Unexpected container size dist, for ing_obj_bus.vni
5328 ERROR: xfc.mpbencode.mpb_be: [MPB-02-1009]:
5329 Unexpected container size dist, for ing_obj_bus.vni
5330 ERROR: xfc.mpbencode.mpb_be: [MPB-02-1009]:
```

| 5331 | Unexpected container size dist, for ing_obj_bus.vni |
|---------|--------------------------------------------------------------------------------------------------------------------|
| 5332 | INFO: xfc.mpbencode.mpb_be: Allocating Encode muxes |
| 5333 | CRITICAL: xfc.main: ( &lt;module&gt;): fc of component mpb failed with exception &lt;class 'typeerror'=""&gt;&lt;/class&gt;&lt;/module&gt; |
| 'NoneTy | ype' object is not subscriptable |

#### Cause

I checked out BE compiler code in file cm/xfc/mpbencode/mpb_be.py, and observed that

- When one field corresponds to multiple containers, error would be reported as shown above. So it seemed that the number of containers VNI takes is the cause for the error. (MY JUDGE FROM CODE, NOT VERIFIED)
- The data passed via MPB is categorized into types: "special_assign", "enc_dec"
 "enc_only", "egr_special_assign", etc.
  - The data passed via MPB in CAN 1.2.12 code has following information

```npl
INFO: main: Executing flexcode generation - mpb
 INFO: xfc.mpbencode.mpb_be: Placement already done...
 INFO: xfc.mpbencode.mpb_be: Starting Flexcode Generation Phase for MPB Encode/Decode
 INFO: xfc.mpbencode.mpb be: Generating CTRL PRE SEL entry
 INFO: xfc.mpbencode.mpb_be: Generating MPB Encode TCAM entries...
 INFO: xfc.mpbencode.mpb_be: Getting Decode PDD bitmap...
 INFO: xfc.mpbencode.mpb_be: Getting Decode to Encode container associations...
 INFO: xfc.mpbencode.mpb_be:ing_bf:'ipost_scratch_bus.event_trace_vector'
 INFO: xfc.mpbencode.mpb_be: type:'special_assign'
 INFO: xfc.mpbencode.mpb be: ing bf:'ipost scratch bus.event trace vector'
 INFO: xfc.mpbencode.mpb_be: type:'special_assign'
 INFO: xfc.mpbencode.mpb_be:ing_bf:'ipost_scratch_bus.event_trace_vector'
 INFO: xfc.mpbencode.mpb\_be: type: 'special\_assign'
 INFO: xfc.mpbencode.mpb_be:ing_bf:'ipost_scratch_bus.drop_code'
 INFO: xfc.mpbencode.mpb_be: type:'special_assign'
 INFO: xfc.mpbencode.mpb_be:ing_bf;'ing_hw_bus.parser_vhlen_0'
 INFO: xfc.mpbencode.mpb_be: type:'special_assign'
 INFO: xfc.mpbencode.mpb_bea: ing_bf:'ing_obj_bus.l3_oif_1'
 INFO: xfc.mpbencode.mpb_be: type:'enc_dec'
 INFO: xfc.mpbencode.mpb_be cont_list: ['ing_obj1__cont_2']
 INFO: xfc.mpbencode.mpb be: len(cont_list): 1
 INFO: xfc.mpbencode.mpb_be:ing_bf:'ing_obj_bus.ifp_class_id'
 INFO: xfc.mpbencode.mpb_be: type:'enc_only'
 INFO: xfc.mpbencode.mpb_be: cont_list: ['ing_obj1__cont_16']
 INFO: xfc.mpbencode.mpb_be: len(cont_list): 1
 INFO: xfc.mpbencode.mpb_be:ing_bf:'ing_obj_bus.l2_iif'
 INFO: xfc.mpbencode.mpb_be: type:'enc_only'
 INFO: xfc,mpbencode.mpb_be: cont_list: ['ing_obj0__cont_9']
 INFO: xfc.mpbencode.mpb_be : len(cont_list): 1
INFO: xfc.mpbencode.mpb_be:ing_bf:'ing_obj_bus.nhop_index_1'
INFO: xfc.mpbencode.mpb_be: type:'enc_dec'
 INFO : xfc.mpbencode.mpb_be : cont_list: ['ing_obj1__cont_15']
 INFO : xfc.mpbencode.mpb_be : len(cont_list): 1
 INFO: xfc.mpbencode.mpb_be:ing_bf:'ing_obj_bus.effective_ttl'
 INFO: xfc.mpbencode.mpb_be: type:'enc_dec'
 INFO: xfc.mpbencode.mpb_be: cont_list: ['ing_obj1__cont_19']
 INFO: xfc.mpbencode.mpb be: len(cont list): 1
 INFO: xfc.mpbencode.mpb_be:ing_bf:'ing_obj_bus.system_source'
 INFO: xfc.mpbencode.mpb_be: type:'enc_dec'
 INFO: xfc.mpbencode.mpb_be: cont_list: ['ing_obj0__cont_13']
 INFO: xfc.mpbencode.mpb be: len(cont list): 1
 INFO: xfc.mpbencode.mpb_be:ing_bf:'ing_obj_bus.erspan3_gbp_sid'
 INFO: xfc.mpbencode.mpb_be: type:'enc_dec'
 INFO: xfc.mpbencode.mpb be: cont list: ['ing obj1 cont 1']
 INFO : xfc.mpbencode.mpb_be : len(cont_list): 1
INFO: xfc.mpbencode.mpb_be: ing_bf:'ing_obj_bus.ingress_qos_remap_value_or_ifp_opaque_obj'
```

```
INFO: xfc.mpbencode.mpb_be: type:'enc_dec'
 INFO: xfc.mpbencode.mpb_be: cont_list: ['ing_obj0__cont_7']
 INFO: xfc.mpbencode.mpb_be: len(cont_list): 1
 INFO: xfc.mpbencode.mpb_be: ing_bf:'ing_obj_bus.ingress_pp_port'
 INFO: xfc.mpbencode.mpb_be: type:'enc_dec'
 INFO: xfc.mpbencode.mpb_be: cont_list: ['ing_obj0__cont_8']
 INFO : xfc.mpbencode.mpb_be : len(cont_list): 1
 INFO: xfc.mpbencode.mpb_be: ing_bf:'ing_obj_bus.vfi'
 INFO: xfc.mpbencode.mpb be: type:'enc dec'
 INFO: xfc.mpbencode.mpb_be: cont_list: ['ing_obj1__cont_12']
 INFO: xfc.mpbencode.mpb_be: len(cont_list): 1
 INFO: xfc.mpbencode.mpb be: ing bf: 'ing_obj_bus.12 oif'
 INFO: xfc.mpbencode.mpb be: type: 'enc dec'
 INFO: xfc.mpbencode.mpb_be: cont_list: ['ing_obj0__cont_19']
 INFO: xfc.mpbencode.mpb be: len(cont list): 1
 INFO: xfc.mpbencode.mpb_be:ing_bf:'ing_cmd_bus.ingress_qos_remark_ctrl'
 INFO : xfc.mpbencode.mpb_be : type:'enc_dec'
 INFO: xfc.mpbencode.mpb_be: cont_list: ['ing_cmd1__cont_34']
 INFO: xfc.mpbencode.mpb_be: len(cont_list): 1
 INFO: xfc.mpbencode.mpb_be:ing_bf:'ing_cmd_bus.tag_action_ctrl'
 INFO: xfc.mpbencode.mpb_be: type:'enc_dec'
 INFO: xfc.mpbencode.mpb be: cont list: ['ing cmd1 cont 19']
 INFO: xfc.mpbencode.mpb_be: len(cont_list): 1
 INFO: xfc.mpbencode.mpb_be:ing_bf:'ing_cmd_bus.system_opcode'
 INFO: xfc.mpbencode.mpb be: type:'enc dec'
 INFO: xfc.mpbencode.mpb be: cont list: ['ing cmd4 cont 7']
 INFO: xfc.mpbencode.mpb_be: len(cont_list): 1
 INFO: xfc.mpbencode.mpb_be: ing_bf:'ing_cmd_bus.int_pri
 INFO: xfc.mpbencode.mpb_be: type:'enc_dec'
 INFO: xfc.mpbencode.mpb_be: cont_list: ['ing_cmd1_
 INFO: xfc.mpbencode.mpb be: len(cont list): 1
 INFO: xfc.mpbencode.mpb\_be: ing\_bf: 'ing\_cmd\_bus.pkt\_misc\_ctrl\_0'
 INFO: xfc.mpbencode.mpb_be: type:'enc_dec' INFO: xfc.mpbencode.mpb_be: cont_list: ['ing_cmd4__cont_3']
 INFO: xfc.mpbencode.mpb_be: len(cont_list): 1
 INFO: xfc.mpbencode.mpb_be:ing_bf.lipost_scratch_bus.event_trace_vector
 INFO : xfc.mpbencode.mpb_be : type:'special_assign' INFO : xfc.mpbencode.mpb_be : ing_bf:'ipost_scratch_bus.event_trace_vector'
 INFO: xfc.mpbencode.mpb_be: type: special_assign'
INFO: xfc.mpbencode.mpb_be: type: special_assign'
INFO: xfc.mpbencode.mpb_be.ring_bf: ipost_scratch_bus.event_trace_vector'
INFO: xfc.mpbencode.mpb_be: type: special_assign'
 INFO: xfc.mpbencode.mpb_be:ing_bf:'ipost_scratch_bus.drop_code'
 INFO: xfc.mpbencode.mpb_be: type:'special_assign'
 INFO: xfc.mpbencode.mpb_be:ing_bf:'ing_hw_bus.parser_vhlen_0'
 INFO: xfc.mpbencode.mpb_be: type:'special_assign'
 INFO: xfc.mpbencode.mpb_be:ing_bf:'ing_obj_bus.ifp_class_id'
 INFO: xfc.mpbencode.mpb_be: type:'enc_only'
 INFO: xfc,mpbencode.mpb_be: cont_list: ['ing_obj1__cont_16']
 INFO : xfc.mpbencode.mpb_be : len(cont_list): 1
NFO: xfc.mpbencode.mpb_be:ing_bf:'ing_obj_bus.l2_iif'
INFO : xfc.mpbencode.mpb_be : type:'enc_only'
 INFO: xfc.mpbencode.mpb_be: cont_list: ['ing_obj0__cont_9']
 INFO: xfc.mpbencode.mpb be: len(cont list): 1
 INFO: xfc.mpbencode.mpb_be:ing_bf:'ing_obj_bus.effective_ttl'
 INFO: xfc.mpbencode.mpb_be: type:'enc_dec'
 INFO: xfc.mpbencode.mpb_be: cont_list: ['ing_obj1__cont_19']
 INFO: xfc.mpbencode.mpb\_be: len(cont\_list): 1\\
 INFO: xfc.mpbencode.mpb be: ing bf:'ing_obj_bus.system source'
 INFO: xfc.mpbencode.mpb_be: type:'enc_dec'
 INFO: xfc.mpbencode.mpb_be: cont_list: ['ing_obj0__cont_13']
 INFO: xfc.mpbencode.mpb_be: len(cont_list): 1
 INFO: xfc.mpbencode.mpb be: ing bf: ing_obj_bus.erspan3 gbp sid
 INFO: xfc.mpbencode.mpb_be: type:'enc_dec'
 INFO: xfc.mpbencode.mpb_be:cont_list:['ing_obj1__cont_1']
 INFO: xfc.mpbencode.mpb be: len(cont list): 1
 INFO: xfc.mpbencode.mpb_be: ing_bf:'ing_obj_bus.ingress_qos_remap_value_or_ifp_opaque_obj'
```

```npl
INFO: xfc.mpbencode.mpb_be: type:'enc_dec'
INFO: xfc.mpbencode.mpb_be: cont_list: ['ing_obj0__cont_7']
INFO: xfc.mpbencode.mpb_be: len(cont_list): 1
INFO: xfc.mpbencode.mpb_be: ing_bf:'ing_obj_bus.ingress_pp_port'
INFO: xfc.mpbencode.mpb_be: type:'enc_dec'
INFO: xfc.mpbencode.mpb_be: cont_list: ['ing_obj0__cont_8']
INFO : xfc.mpbencode.mpb_be : len(cont_list): 1
INFO: xfc.mpbencode.mpb_be:ing_bf:'ing_obj_bus.vfi'
INFO: xfc.mpbencode.mpb be: type:'enc dec'
INFO: xfc.mpbencode.mpb_be: cont_list: ['ing_obj1__cont_12']
INFO: xfc.mpbencode.mpb_be: len(cont_list): 1
INFO: xfc.mpbencode.mpb be: ing bf: 'ing_obj_bus.12 oif'
INFO: xfc.mpbencode.mpb_be: type:'enc_dec'
INFO: xfc.mpbencode.mpb_be: cont_list: ['ing_obj0__cont_19']
INFO: xfc.mpbencode.mpb be: len(cont list): 1
INFO: xfc.mpbencode.mpb_be:ing_bf:'ing_cmd_bus.ingress_qos_remark_ctrl'
INFO : xfc.mpbencode.mpb_be : type:'enc_dec'
INFO: xfc.mpbencode.mpb be: cont list: ['ing cmd1 cont 34']
INFO : xfc.mpbencode.mpb_be : len(cont_list): 1
INFO: xfc.mpbencode.mpb_be:ing_bf:'ing_cmd_bus.tag_action_ctrl'
INFO: xfc.mpbencode.mpb_be: type:'enc_dec'
INFO: xfc.mpbencode.mpb be: cont list: ['ing cmd1 cont 19']
INFO : xfc.mpbencode.mpb_be : len(cont_list): 1
INFO: xfc.mpbencode.mpb_be:ing_bf:'ing_cmd_bus.system_opcode'
INFO: xfc.mpbencode.mpb be: type:'enc dec'
INFO: xfc.mpbencode.mpb_be: cont_list: ['ing_cmd4__cont_7']
INFO : xfc.mpbencode.mpb_be : len(cont_list): 1
INFO: xfc.mpbencode.mpb_be: ing_bf:'ing_cmd_bus.int_pri
INFO: xfc.mpbencode.mpb_be: type:'enc_dec'
INFO: xfc.mpbencode.mpb_be: cont_list: ['ing_cmd1_
INFO: xfc.mpbencode.mpb be: len(cont list): 1
INFO: xfc.mpbencode.mpb_be:ing_bf:'ing_cmd_bgs.pkt_misc_ctrl_0'
INFO : xfc.mpbencode.mpb_be : type:'enc_dec'
INFO : xfc.mpbencode.mpb_be : cont_list: ['ing_cmd4__cont_3']
INFO: xfc.mpbencode.mpb_be: len(cont_list): 1
INFO: xfc.mpbencode.mpb_be: Allocating Encode muxes...
INFO: main: bus mapping phase
```

#### Solution

- It seems NCSP 1.3.15 corrects the problem, but NCSP 1.3.14rc1 does not. (Need to confirm)
  - It seems the fix is related to the issues reported by Ruijie.
- BE related code o get_dec_to_enc_mapping()@cm/xfc/mpbencode/mpb_be.py

# 4 Flex State

## 4.1 Problem: SDKLT FLEX_STATE_EGR_ACTION_PROFILE.Obj0 Inconsistent with

- NCSP: NCSP_INTERNAL_M_6.5.30.8

From map.yml, egr0 objects should look like this:

```npl
flex_ctr_st_egr0_objects:
 object_template:
 '0': !!python/object/apply:collections.OrderedDict

 - PHYSICAL_INDEX

 - [0]
 - PROFILE_CALL
 - {in_egr_obj_bus_sharp_pool_id: egr_obj_bus.sharp_pool_id}
 - {in_egr_cmd_bus_sharp_pass_id: egr_cmd_bus.sharp_pass_id}
 - {in_egr_obj_bus_sharp_worker_id: egr_obj_bus.sharp_worker_id}
```

However, ran table dump in SDKLT shell and saw difference:

```npl
+t lietas (i veh-rhhe lieta).
 OBJ
 Width: 8 bits
 Attribute: R/W, symbol, array[4]
 Symbol: 19
 SHARP_DATA_PART2_DATA0_0 SHARP_DATA_PART2_DATA3_0
 SHARP_DATA_PART2_DATA0_1 SHARP_DATA_PART2_DATA3_1
 SHARP DATA PART3 DATA2 0 SHARP DATA PART3 DATA0 0
 SHARP_DATA_PART3_DATA2_1 SHARP_DATA_PART3_DATA0_
 SHARP_DATA_PART3_DATA1_0 SHARP POOL ID
 SHARP DATA PART3 DATA1 1 SHARP DATA PART3 DATAS
 SHARP DATA PART2 DATA1 0 SHARP DATA PART3 DATA
 SHARP_DATA_PART2_DATA1_1 SHARP_TYPE
 SHARP_DATA_PART2_DATA2_0 SHARP_STATE1_IMDEX
 SHARP DATA PART2 DATA2 1
 Default value: SHARP DATA PART2 DATA0
 OBJ0
 Width: 8 bits
 Attribute: R/W, symbol, array
 Symbol: 2
 SHARP PASS ID SHARP TY
 Default value: SHARP PASS
```

- Cause: SDK localized the cause that sharp_pool_id and sharp_worker_id does not have matched cycle with egress F\$ instance 0 in bus_be_output.yml. SDK pointed out the cause that the two fields are extracted from header by eparser1, and eparser1 is at the same stage as egress FS instance 0, so there is no egress-fs-instance-0 cycles for sharp pool id and sharp worker id.
- Solution: change NPL to pass the two fields using MPB instead of extracting them via parser.

## 5.1 Problem: cnstr of component pr failed with exception `&lt;class 'KeyError'&gt;`

NCSP: 1.3.15rc1

**Problem**

INFO: main: Executing constraint - pr

INFO: xfc.parser_parser_be: Doing Placement...

INFO: xfc.parser.parser_be: Creating Eparser Extraction DB for ingress_pkt.outer_I2_hdr

(populate_egr_field_bus)

```npl
INFO: xfc.parser.parser_be: Creating Eparser Extraction DB for ingress_pkt.outer_I3_I4_hdr
(populate_egr_field_bus)
INFO: xfc.parser.parser be: Creating Eparser Extraction DB for ingress pkt.inner I2 hdr
(populate egr field bus)
INFO: xfc.parser.parser be: Creating Eparser Extraction DB for ingress pkt.inner 13 14 hdr
(populate egr field bus)
INFO: xfc.parser_parser_be: Running first pass for HME Placement
 CRITICAL: main: (critical): cnstr of component pr failed with exception <class 'KeyError'>
'inner ali hdr'
make lint
```

Code the cause the problem

```npl
struct ali_hdr_t {
 fields {
 bit[128] ali fld 0;
 ali_fld_1;
 bit[8]
 bit[8]
 ali_fld_2;
 bit[16] ali fld reserve;
 }
}
parser node vxlan {
 extract_fields(ingress_pkt.outer_I3_I4_hdr.vxlan);
 parser1 field bus.vxlan vn id reserved 2[7:0] =
ingress_pkt.outer_I3_I4_hdr.vxlan.reserved2
 switch(latest.reserved2) {
 0x3 : parse break(inner ethernet);
 0x5 : parse_break(inner_ali_hdr);
 default : next_node ingress
 }
}
parser_node inner_ali hdr {
 extract_fields(ingress_pkt.inner_I3_I4_hdr.ali_hdr);
 parser2_field_bus.ip_hdr_dip = ingress_pkt.inner_I3_I4_hdr.ali_hdr.ali_fld_0;
 next_node ingress;
```

## 5.2 Problem: writing to the same bus field

NCSP: 1.3.15rc1

Problem

```npl
Writing IR to file:
 fe output/ir/ir.yml
INFO: Post DFG Target Specific Checks
ERROR: [TAC2-02-1010]:
```

Bus.field is being overwritten by down stream node, Parser nodes ipv6 and ali_hdr are writing to the same bus field parser1_field_bus.ip_hdr_dip offset 0 8bit granularity

 Cause: As per the prompt, multiple extractions are NOT allowed to write into the same destination container

## 5.3 Problem: No Enough HFE Commands

NCSP: NCSP_INTERNAL_M_6.5.30.8

- Problem: shown below

ERROR : xfc.parser.parser_be : [PR-03-1003] :
Not enough HFE commands found, stage 2

- Cause: it seems HFE commands are close to exhaustion
- The code to pass the compilation

Before: (extract 16b)

parser2_field_bus.sharp_reserve = ingress_pkt.l5_l6_hdr.in_nwk_comp.reserve;

After: (extract only 8b)

parser2_field_bus.sharp_reserve = ingress_pkt.l5_l6_hdr.in_nwk_comp.reserve[15:8];

- Solution
 - Each stage has limited number of HFE commands as IPARSERx_HFE_POLICY_TABLE_y shows
 - NPL provided one directive "force_new_hme" to force the use of new HME stage. So if "No Enough HFE Commands" is reported for one HME stage, "force_new_hme" can be used to force use of new stage to avoid the shortage of HFE commands.

## 5.4 Problem: Match ID Logic Absent

- NCSP: NCSP INTERNAL M 6.5.30.4
- Problem: H3C INC packet format as shown below need to have 190B header parsed, then use
 one or two parser instance and one header group to parse the 190B header, and following FE
 compilation errors were reported:

INFO: Generating DFG for Functions

ERROR: Error running Fsl-Dfg for 'wrap__fsl1_functions'

Log: 'Match ID logic absent for header egress_pkt.fwd_I3_I4_hdr.sharp_data_part2 at /projects/xfc/yanguang/depot/yq959556_NCS_INTERNAL_M_6.5.30.4/xgsflexcompiler/npl_fe/dfg/fsl/fsl.pl line 1745.'

ERROR: Error running Fsl-Dfg for 'wrap_uat0_fsl_0'

Log: Match ID logic absent for header ingress_pkt.outer_l3_l4_hdr.sharp_data_part0 at /projects/xfc/yanguang/depot/yq959556_NCS_INTERNAL_M_6.5.30.4/xgsflexcompiler/npl_fe/dfg/fsl/fsl.pl line 1745.

ERROR: Error running Fsl-Dfg for 'wrap uat1 fsl 0'

Log: 'Match ID logic absent for header ingress_pkt.outer_13_14_hdr.sharp_data_part4 at /projects/xfc/yanguang/depot/yq959556_NCS_INTERNAL_M_6.5.30.4/xgsflexcompiler/npl_fe/dfg/fsl/fsl.pl line 1745.'

INFO: Fsl Dfg execution failed.

Compilation Failed...

DA

#### RoCEv2 packet format

|  | 1 |
|------------|---|
| Eth header |  |
| IP header |  |
| UDP header |  |
| BTH |  |
| RETH |  |
| IMMDT |  |
| INC header |  |
| Payload |  |
| ICRC |  |
| FCS |  |
| | |

  - Cause: unclear.
  - Workaround: After I expanded the header group from one to two, the compilation error disappeared.

## 5.5 Problem: Parser Max Placement Issue

- NCSP: 1.3.15rc1
- Problem Description: following error was observed in backend NPL compilation.

```
INFO: xfc.bus.bus_be: Placed field: parser1_field_bus.a_psn[1247:1224]
 Placed index/Max index: 160/441<24>
INFO: xfc.bus.bus_be: logical consumer producer TAP Points
 CONS: PARSER.rdma bth.OUT
 PROD: wrap uat0 fsl 0.IN
 INFO: xfc.bus.bus be: physical consumer producer TAP Points
 CONS: iparser1.OUT
 PROD: ifsl40.IN
 INFO: xfc.bus.bus_be: Placed field: parser1_field_bus.a_psn[1247:1228]
 Placed index/Max index: 161/441<20>
INFO: xfc.bus.bus be: logical consumer producer TAP Points
 CONS: PARSER.rdma_bth.OUT
 PROD: wrap_uat0_fsl_0.IN
INFO: xfc.bus.bus be: physical consumer producer TAP Points
 CONS: iparser1.OUT
 PROD: ifsl40.IN
 INFO: xfc.bus.bus be: Placed field: parser1 field bus.a psn[1247:1232]
 Placed index/Max index: 162/441<16>
 INFO: xfc.bus.bus be: logical consumer producer TAP Points
 CONS: PARSER.rdma bth.OUT
PROD: wrap__uat0_fsl_0.IN
MNFO: xfc.bus.bus be: physical consumer producer TAP Points
 CONS: iparser1.OUT
 PROD: ifsI40.IN
INFO: xfc.bus.bus_be: Placed field: parser1_field_bus.a_psn[1247:1236]
 Placed index/Max index: 163/441<12>
 INFO: xfc.bus.bus be: logical consumer producer TAP Points
 CONS: PARSER.rdma bth.OUT
 PROD: wrap uat0 fsl 0.IN
 INFO: xfc.bus.bus be: physical consumer producer TAP Points
 CONS: iparser1.OUT
```

```npl
PROD: ifsI40.IN
 INFO: xfc.bus.bus be: Placed field: parser1 field bus.a psn[1247:1240]
 Placed index/Max index: 164/441<8>
 INFO: xfc.bus.bus be: logical consumer producer TAP Points
 Index Index
 CONS: PARSER.rdma bth.OUT
 PROD: wrap uat0 fsl 0.IN
 INFO: xfc.bus.bus_be: physical consumer producer TAP Points
 CONS: iparser1.OUT
 PROD: ifsI40.IN
 ERROR: xfc.bus.bus be: [BUS-01-1004]:
 Bus placement failed, Placement Unsuccessful. Max Placement depth = 166
 INFO: xfc.bus.bus_be: Creating Final Output...
 INFO: xfc.bus.bus_be: Adjusting mapping for MOP init
 INFO: xfc.bus.bus be: MOP init fields: []
 INFO: xfc.bus.bus be: Collector Container Map: {}
Compiler run FAILED. Detected: (1)Errors and (61)Warnings
make lint
```

#### Relevant NPL code

Code causing problems

```npl
parser node rdma bth {
 extract_fields(ingress_pkt.outer_I3_I4_hdr.rdma_bth);
 parser1_field_bus.rdma_opcode = ingress_pkt.outer_l3_l4_hdr.rdma_bth.opcode;
 parser1_field_bus.rdma_dstqp = ingress_pkt.outer_l3_l4_hdr.rdma_bth.dstqp;
 ing_cmd_bus.rdma_ackreq = ingress_pkt.outer_I3_I4_hdr.rdma_bth.ackreq;
 ing_obj_bus.rdma_psn = ingress_pkt.outer_l32l4_hdr.rdma_bth.psn;
 switch (latest.opcode) {
 // RC RDMA Write First
 0x06: next_node rdma_reth;
 0x0A mask 0xFE: next_node_rdma_reth; // RC RDMA Write Only
 0x0C: next _node rdma_reth;
 // RC RDMA Read Request
 // RC RDMA Read Response First
 0x0D: next_node rdma_aeth;
 0x0F: next node rdma aeth;
 // RC RDMA Read Response Last
 0x10 mask 0xFE: next_node rdma_aeth; // RC RDMA Read Response Only, Acknowledge
 0x64: next_node rdma_deth;
 // UD Send Only
 default: next_node ingress;
```

Code fixing the problems

```npl
parser_node rdma_bth {
 extract_fields(ingress_pkt.outer_l3_l4_hdr.rdma_bth);
 parser1_field_bus.rdma_opcode_dstqp[31:24] = ingress_pkt.outer_l3_l4_hdr.rdma_bth.opcode;
 parser1_field_bus.rdma_opcode_dstqp[23:0] = ingress_pkt.outer_l3_l4_hdr.rdma_bth.dstqp;
 parser1_field_bus.rdma_ackreq_res_psn[31:24] = ingress_pkt.outer_l3_l4_hdr.rdma_bth.ackreq_res;
 parser1_field_bus.rdma_ackreq_res_psn[23:0] = ingress_pkt.outer_l3_l4_hdr.rdma_bth.psn;
 switch (latest.opcode) {
 0x06: next_node rdma_reth;
 // RC RDMA Write First
 0x0A mask 0xFE: next_node rdma_reth; // RC RDMA Write Only
 0x0C: next_node rdma_reth;
 // RC RDMA Read Request
 0x0D: next_node rdma_aeth;
 // RC RDMA Read Response First
 0x0F: next_node rdma_aeth;
 // RC RDMA Read Response Last
```

```npl
Ox10 mask 0xFE: next_node rdma_aeth; // RC RDMA Read Response Only, Acknowledge
```

#### Cause

  - It's unclear for exact cause.
- After replacing "ing_cmd_bus.rdma_ackreq" with "parser1_field_bus.rdma_ackreq" and
 "ing_obj_bus.rdma_psn" with "parser1_field_bus.rdma_psn", the problem was fixed
- Also declare 32b parser1_field_bus fields, and pack extracted fields into the 32b parser1_field_bus. Next, if needed, FSL can convey data from parser1_field_bus to ing_obj_bus or ing_cmd_bus.

## Debug Method

  - Fall back all code changes and keep only parser related code, and also add extracted bus fields into IFP. In this way, it's easy to localize the fields causing the problems.
- Comparing log between successful compilation and failed one shows that BE compiler went into exception after adding field extraction of "ing_cmd_bus.rdma_ackreq"

# 6 FSI

## 6.1 Problem: Pre placement checks failed

NCSP: 1.3.15rc1 or 1.3.15rc2

Problem:

INFO: xfc.bus.bus_be: Running pre-placement checks...

ERROR: xfc.bus.bus_be: [BUS-02-1001]:

Not enough common container connected, Insufficient container width for field pkt_fwd_field_bus.ip_hdr_dip: Field Width = 128 bit(s), Total available cont width = 0 bit(s)

CRITICAL: xfc.bus.bus_be: (critical): Pre placement checks failed make lint

- The code to cause the problem

```npl
// In IFSL70 or IFSL71
pkt_fwd_field_bus.ip_hdr_dip[31:0] = parser1_field_bus.ali_fld_0;
```

Problem Cause: from fsl_hcf.yaml, IFSL70 and IFSL71 does not support field bus as output, so
object bus or command bus will be allocated to hold the field bus data, so container will be insufficient.

## 6.2 Problem: Unplaced logical register

NCSP: NCSP_INTERNAL_M_6.5.30.8

Problem

```npl
CRITICAL: fsl: (execute_command): fsl_profile.pl:9476: Unplaced logical register | Logical Signal r_egr_sharp_config.worker_id_full_bmp[7:0] | Bus DATA | Bus Type MAIN | Floor 0

CRITICAL: fsl: (execute_command): fsl_instance.pl:325: All engines failed for efsl30.wrap__fsl2_functions

ERROR: fsl: [FSL-01-1001]:

PERL-ERROR, [FATAL] main::fsl_instance(efsl30) failed with rc=256

CRITICAL: fsl: (critical): Command execution failed. Unable to continue
```

The code to cause the problem

 $\label{eq:completed} $$ \operatorname{egr_fsl2_local_bus.all_worker_completed} = ((\operatorname{egr_obj_bus.sharp_worker_bitmap[15:0]} == \\ r_{\operatorname{egr_sharp_config.worker_id_full_bmp[15:0]}) \&\& (\operatorname{egr_obj_bus.sharp_worker_bitmap[31:16]} == \\ r_{\operatorname{egr_sharp_config.worker_id_full_bmp[31:16]}));$

- Problem Cause: it seems bus containers need to firstly be allocated, but there is no enough containers to allocate.
- The code to pass the compilation

```npl
Prior FSL Stage:
\negr_obj_bus.worker_id_full_bmp_lo = r_egr_sharp_config.worker_id_full_bmp_lo;
\negr_obj_bus.worker_id_full_bmp_hi = r_egr_sharp_config.worker_id_full_bmp_hi;

Current FSL Stage:
\negr_fsl2_local_bus.all_worker_completed = 0;
\nif ((egr_obj_bus.sharp_worker_bitmap[15:0] == egr_obj_bus.worker_id_full_bmp_lo) &&

(egr_obj_bus.sharp_worker_bitmap[31:16] == egr_obj_bus.worker_id_full_bmp_hi))
\negr_fsl2_local_bus.all_worker_completed = 1;
```

## 6.3 Problem: Concat unsupported

- NCSP: NCSP_INTERNAL_M_6.5.30.4
- Problem:

```npl
INFO: fsl: [FN] Programming function wrap__fsl2_functions...
INFO: fsl: [TM] Reading top map...
INFO: fsl: [HC] Reading FSL HCF...
INFO: fsl: [IR] Reading IR...
INFO: fsl: [IR] Processing function...

CRITICAL: fsl: (execute_command): fsl_profile.pl:9476: Concat not supported | Logical Signal __NET_32[1:0]

CRITICAL: fsl: (execute_command): fsl_instance.pl:325: All engines failed for efsl30.wrap__fsl2_functions

ERROR: fsl: [FSL-01-1001]:

PERL-ERROR, [FATAL] main::fsl_instance(efsl30) failed with rc=256

CRITICAL: fsl: (critical): Command execution failed. Unable to continue
```

- Code to cause the problem

```npl
egr_obj_bus.sharp_new_reserve[9:0] = egr_field_bus.sharp_reserve[9:0];\negr_obj_bus.sharp_new_reserve[13:10] = egr_cmd_bus.next_sharp_pass_id;\negr_obj_bus.sharp_new_reserve[15:14] = egr_cmd_bus.next_sharp_type[1:0];
```

- Problem Cause:

It seems that data copy is done in the unit of 4b container. So the container has to get aligned at 4b boundary.

The code to pass the compilation

```npl
egr_obj_bus.sharp_new_reserve[7:0] = egr_field_bus.sharp_reserve[7:0];\negr_obj_bus.sharp_new_reserve[11:8] = egr_cmd_bus.next_sharp_pass_id;\negr_obj_bus.sharp_new_reserve[15:12] = egr_cmd_bus.next_sharp_type[3:0];
```

# 7 SDKLT Compilation Problem: NPL Code related Cause

- NCSP: NCSP INTERNAL M 6.5.30.4

- Problem: NPL compilation passed, but SDKLT compilation failed.
   - Cause: Action fields resolved from table my_station_table were NOT consumed.
   - Solution: Removed the use of my station table, and SDKLT compilation passed.

# 8 SDK LTT Problem: NPL code related cause

- NCSP: NCSP_INTERNAL_M_6.5.30.4
- Problem: NPL compilation passed, but SDK LTT failed as shown below

```
bin/xfcr \
 -O /projects/xfc/yanguang/git/gerrit/ngsdk_ali_cc/bcmcth -N
/projects/xfc/yanguang/git/gerrit/ngsdk_ali_cc/INTERNAL/fltg -L
/projects/xfc/yanguang/git/gerrit/ngsdk_ali_cc/tools/fltg -X
/projects/xfc/yanguang/git/gerrit/ngsdk ali cc/tools/fltg \
 -P /projects/xfc/yanguang/git/gerrit/ngsdk_ali_cc/INTERNAL/fltg/ptrm -T
/projects/xfc/yanguang/git/gerrit/ngsdk_ali_cc/tools/fltg/ptrm \
 -c bcm56780 a0 -V sharp \
 -f \ npl/bcm56780\_a0/sharp/logical\_sftblfile.yml-g \ npl/bcm56780\_a0/sharp/logical\_regsfile.yml-m
/projects/xfc/yanguang/git/gerrit/ngsdk_ali_cc/tools/fltg/generated/npl/bcm56780_a0_sharp_map.yml -i
npl/bcm56780 a0/sharp/ifile.yml \
Invalid container info Depth for dt_vfp_action_template at
/projects/xfc/yanguang/git/gerrit/ngsdk_ali_cc/INTERNAL/fltg/bin/.\(\)(ib/Flex/Module/Custom.pm line 41.
make[3]: *** [Makefile.npl:391: sf_tables] Error 255
make[3]: Leaving directory '/projects/xfc/yanguang/git/gerrit/ngsdk_ali_cc/INTERNAL/fltg'
make[2]: *** [Makefile.npl:175: reinvoke bcm56780 a0#sharp] Error 2
make[2]: Leaving directory '/projects/xfc/yanguang/git/gerrit/ngsdk_ali_cc/INTERNAL/fltg'
make[1]: *** [Makefile.npl:160: all] Error 2
make[1]: *** [Makefile.npl:160: all] Error 2
make[1]: Leaving directory '/projects/xfc/yanguang/git/gerrit/ngsdk_ali_cc/INTERNAL/fltg'
make: *** [npl/npl.mk:337: integrate_npl] Error 2
```

- Cause: In NPL code, the fields present in dt_vfp_action_template were NOT consumed.
- Solution: Added some processing FSL to consume the fields output from dt_vfp_action_template.
- Takeway: Error messages gave some information on the possible cause, and NPL code can be checked according to the error information.
