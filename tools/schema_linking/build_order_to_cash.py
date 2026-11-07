"""Deterministic synthetic order-to-cash metadata, no business rows or answers."""
from pathlib import Path
import json,hashlib
ROOT=Path(__file__).resolve().parents[2]
# One connected business schema: master data -> sales -> fulfilment -> cash,
# with purchasing replenishment and explicit current/history/detail/summary grains.
TABLES=[
('customer','客户档案','客户,客商','customer_name:客户名称:TEXT,segment_id:分群编号:INTEGER>customer_segment'),
('customer_segment','客户分群','客群','segment_name:分群名称:TEXT'),
('customer_contact','客户联系人','联络人','customer_id:客户编号:INTEGER>customer,contact_name:联系人姓名:TEXT'),
('customer_address','客户地址','收货地址','customer_id:客户编号:INTEGER>customer,address_text:地址正文:TEXT'),
('product_category','商品分类','品类','category_name:分类名称:TEXT'),
('product','商品档案','货品,SKU','category_id:分类编号:INTEGER>product_category,product_name:商品名称:TEXT,unit_price:单价:NUMERIC'),
('warehouse','仓库档案','库房','warehouse_name:仓库名称:TEXT'),
('quotation','销售报价单','报价单','customer_id:客户编号:INTEGER>customer,quoted_amount:报价金额:NUMERIC'),
('quotation_line','报价明细','报价行','quotation_id:报价单编号:INTEGER>quotation,product_id:商品编号:INTEGER>product,quantity:报价数量:NUMERIC'),
('sales_order','当前销售订单','销售单,订单','customer_id:客户编号:INTEGER>customer,quotation_id:报价单编号:INTEGER>quotation,order_amount:订单金额:NUMERIC,order_status:订单状态:TEXT'),
('sales_order_history','历史销售订单','历史订单,订单归档,订单','original_order_id:原订单编号:INTEGER>sales_order,customer_id:客户编号:INTEGER>customer,order_amount:历史订单金额:NUMERIC,archived_at:归档时间:TEXT'),
('sales_order_line','销售订单明细','销售订单行,订单明细','order_id:订单编号:INTEGER>sales_order,product_id:商品编号:INTEGER>product,quantity:订购数量:NUMERIC,line_amount:明细金额:NUMERIC'),
('sales_order_line_history','历史订单明细','归档订单行','history_id:历史订单编号:INTEGER>sales_order_history,product_id:商品编号:INTEGER>product,quantity:历史订购数量:NUMERIC'),
('sales_order_daily_summary','销售订单日汇总','每日订单汇总,日订单统计','customer_id:客户编号:INTEGER>customer,business_date:业务日期:TEXT,order_count:订单笔数:INTEGER,order_amount:订单汇总金额:NUMERIC'),
('sales_order_monthly_summary','销售订单月汇总','每月订单汇总,月订单统计','customer_id:客户编号:INTEGER>customer,business_month:业务月份:TEXT,order_count:订单笔数:INTEGER,order_amount:订单汇总金额:NUMERIC'),
('shipment','销售发货单','出库发货单,发货单','order_id:订单编号:INTEGER>sales_order,warehouse_id:仓库编号:INTEGER>warehouse,shipped_at:发货时间:TEXT'),
('shipment_line','发货明细','发货行','shipment_id:发货单编号:INTEGER>shipment,order_line_id:订单行编号:INTEGER>sales_order_line,shipped_quantity:实发数量:NUMERIC'),
('shipment_tracking','物流轨迹','物流事件','shipment_id:发货单编号:INTEGER>shipment,event_time:轨迹时间:TEXT,event_text:轨迹内容:TEXT'),
('sales_return','销售退货单','客户退货,退货单','order_id:订单编号:INTEGER>sales_order,customer_id:客户编号:INTEGER>customer,return_reason:退货原因:TEXT'),
('sales_return_line','退货明细','退货行','return_id:退货单编号:INTEGER>sales_return,product_id:商品编号:INTEGER>product,returned_quantity:实退数量:NUMERIC'),
('invoice','销售发票','开票记录,销项发票','order_id:订单编号:INTEGER>sales_order,customer_id:客户编号:INTEGER>customer,invoiced_amount:开票金额:NUMERIC'),
('invoice_line','发票明细','开票明细','invoice_id:发票编号:INTEGER>invoice,order_line_id:订单行编号:INTEGER>sales_order_line,tax_amount:税额:NUMERIC'),
('credit_note','销售红字单','红字发票,贷项通知','invoice_id:发票编号:INTEGER>invoice,return_id:退货单编号:INTEGER>sales_return,credit_amount:红字金额:NUMERIC'),
('receivable','应收账款','应收,待收款','invoice_id:发票编号:INTEGER>invoice,customer_id:客户编号:INTEGER>customer,receivable_amount:应收金额:NUMERIC,due_date:到期日:TEXT'),
('receivable_installment','应收分期','分期应收','receivable_id:应收编号:INTEGER>receivable,due_date:分期到期日:TEXT,installment_amount:分期金额:NUMERIC'),
('receipt','实收款记录','实收,到账收款','customer_id:客户编号:INTEGER>customer,received_amount:实收金额:NUMERIC,received_at:到账时间:TEXT'),
('receipt_allocation','收款核销','应收核销,收款分配','receipt_id:收款编号:INTEGER>receipt,receivable_id:应收编号:INTEGER>receivable,allocated_amount:核销金额:NUMERIC'),
('refund','退款记录','销售退款','return_id:退货单编号:INTEGER>sales_return,customer_id:客户编号:INTEGER>customer,refund_amount:退款金额:NUMERIC'),
('refund_allocation','退款分摊','退款分配','refund_id:退款编号:INTEGER>refund,receipt_id:收款编号:INTEGER>receipt,allocated_amount:分摊金额:NUMERIC'),
('inventory_balance','库存余额','现存量,库存快照','warehouse_id:仓库编号:INTEGER>warehouse,product_id:商品编号:INTEGER>product,on_hand_quantity:现存数量:NUMERIC'),
('inventory_movement','库存流水','库存变动,出入库流水','warehouse_id:仓库编号:INTEGER>warehouse,product_id:商品编号:INTEGER>product,movement_quantity:变动数量:NUMERIC'),
('stock_reservation','库存预留','订单占用库存,库存锁定','order_line_id:订单行编号:INTEGER>sales_order_line,warehouse_id:仓库编号:INTEGER>warehouse,reserved_quantity:预留数量:NUMERIC'),
('supplier','供应商档案','供货商','supplier_name:供应商名称:TEXT'),
('purchase_order','采购订单','采购单','supplier_id:供应商编号:INTEGER>supplier,purchase_amount:采购金额:NUMERIC'),
('purchase_order_line','采购明细','采购订单行','purchase_id:采购单编号:INTEGER>purchase_order,product_id:商品编号:INTEGER>product,quantity:采购数量:NUMERIC'),
('goods_receipt','采购入库单','到货入库单','purchase_id:采购单编号:INTEGER>purchase_order,warehouse_id:仓库编号:INTEGER>warehouse,received_at:入库时间:TEXT'),
('goods_receipt_line','采购入库明细','到货明细','goods_receipt_id:入库单编号:INTEGER>goods_receipt,purchase_line_id:采购行编号:INTEGER>purchase_order_line,quantity:入库数量:NUMERIC'),
('payable','应付账款','应付,供应商应付款','purchase_id:采购单编号:INTEGER>purchase_order,supplier_id:供应商编号:INTEGER>supplier,payable_amount:应付金额:NUMERIC'),
('payment','供应商付款','采购付款,实付','payable_id:应付编号:INTEGER>payable,supplier_id:供应商编号:INTEGER>supplier,paid_amount:实付金额:NUMERIC')]

def build(version="1.0.0"):
    if version not in ("1.0.0","1.1.0"):raise ValueError("unsupported synthetic metadata version")
    target=ROOT/('domain-packs/order_to_cash/v1' if version=='1.0.0' else 'domain-packs/order_to_cash/v1.1');target.mkdir(parents=True,exist_ok=True)
    source='source.order_to_cash_synthetic'
    concepts=[];aliases=[];objects=[];relations=[];ddl=[]
    def obj(oid,kind,parent,label,physical,datatype=None,refs=()):
        return dict(object_id=oid,object_type=kind,parent_object_id=parent,display_name=label,physical_name=physical,data_type=datatype,concept_refs=list(refs),authorization_level='read_public',source_refs=[source])
    objects.append(obj('schema.order_to_cash','data_source',None,'订单到回款合成业务库','order_to_cash'))
    for name,label,synonyms,columns in TABLES:
        cid='concept.otc.'+name;tid='schema.otc.'+name
        concepts.append(dict(concept_id=cid,display_name=label,description='合成订单到回款业务对象：'+label+'，与历史、明细、汇总口径严格区分。',parent_concept_ids=[],source_refs=[source]))
        for index,term in enumerate(dict.fromkeys([label,name,*synonyms.split(',')])):
            aliases.append(dict(alias_id='alias.otc.'+name+'.'+str(index),surface_form=term,language='en' if term.isascii() else 'zh-CN',concept_id=cid,source_ref=source,confidence_class='synthetic'))
        objects.append(obj(tid,'table','schema.order_to_cash',label,name,refs=[cid]))
        definitions=['id:记录编号:INTEGER',*columns.split(','),'created_at:创建时间:TEXT']
        definitions=list(dict((x.split(':')[0],x) for x in definitions).values())
        sql=[];foreign=[]
        for definition in definitions:
            physical,display,kind=definition.split(':');datatype,_,reference=kind.partition('>')
            oid=tid+'.'+physical
            objects.append(obj(oid,'column',tid,display,physical,datatype,refs=[cid]))
            sql.append('"'+physical+'" '+datatype+(' PRIMARY KEY' if physical=='id' else ''))
            if reference:
                foreign.append('FOREIGN KEY("'+physical+'") REFERENCES "'+reference+'"("id")')
                relations.append(dict(relation_id='relation.otc.'+name+'.'+physical,relation_type='foreign_key',source_object_id=oid,target_object_id='schema.otc.'+reference+'.id',direction='directed',cardinality='many_to_one',source_refs=[source]))
        ddl.append('CREATE TABLE "'+name+'" ('+', '.join(sql+foreign)+');')
    artifacts={'concepts':concepts,'aliases':aliases,'slots':[],'schema_catalog':objects,'relations':relations,'value_evidence':[],'validation_rules':[]}
    if version=='1.1.0':
        artifacts['slots']=[dict(slot_id='slot.schema_target',display_name='目标表与数据粒度',description='表或粒度不明确时选择定位目标；不是SQL执行授权。',value_type='enum',required_by_default=False,required_when=[],conflict_slot_ids=[],allowed_value_evidence_refs=['evidence.otc.schema_targets'],source_refs=[source])]
        artifacts['value_evidence']=[dict(evidence_id='evidence.otc.schema_targets',slot_id='slot.schema_target',evidence_type='enum',payload={'values':[table[0] for table in TABLES]},data_level='L0',send_to_model=True,source_ref=source)]
    names={'schema_catalog':'schema-catalog.json','value_evidence':'value-evidence.json','validation_rules':'validation-rules.json'}
    files=[]
    for role,items in artifacts.items():
        filename=names.get(role,role+'.json');data={'schema_version':'1.0.0','artifact_type':role,'items':items}
        (target/filename).write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
        files.append(dict(role=role,path=filename,sha256=hashlib.sha256((target/filename).read_bytes()).hexdigest(),data_level='L0',send_to_model=True))
    notice='Synthetic order-to-cash, fulfilment, inventory and replenishment metadata. No business rows, questions, scoring labels or credentials. Created for public development; no unseen evaluation claim.\n'
    (target/'LICENSE-or-SOURCE.md').write_text(notice,encoding='utf-8',newline='\n')
    files.append(dict(role='license_notice',path='LICENSE-or-SOURCE.md',sha256=hashlib.sha256(notice.encode()).hexdigest(),data_level='L0',send_to_model=False))
    manifest=dict(domain_pack_id='order_to_cash',domain_id='order_to_cash',version=version,schema_version='1.0.0',content_schema_id='domain-pack-content.v1',language='zh-CN',created_at='2026-10-25T00:00:00Z',data_level='L0',source_refs=[dict(source_id=source,uri='repo:domain-packs/order_to_cash/v1/LICENSE-or-SOURCE.md',license='MIT',data_level='L0',authorized=True)],license=dict(spdx_id='MIT',redistribution_allowed=True,notice_path='LICENSE-or-SOURCE.md'),compatibility=dict(task_types=['nl2sql'],database_versions=['order-to-cash-schema.v1']),hash_algorithm='sha256-utf8-lf',files=files)
    (target/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
    (ROOT/'data/schema-linking/order_to_cash/v1/schema.sql').write_text('\n'.join(ddl)+'\n',encoding='utf-8',newline='\n')
    print({'tables':len(TABLES),'objects':len(objects),'foreign_keys':len(relations)})

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--version',default='1.0.0',choices=['1.0.0','1.1.0']);args=parser.parse_args();build(args.version)
