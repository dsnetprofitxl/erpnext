# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# MIT License. See license.txt

# For license information, please see license.txt


import json

import frappe
from frappe.model.document import Document
from frappe.utils import flt

from erpnext.stock.get_item_details import get_item_details


class PackedItem(Document):
	pass


def make_packing_list(doc):
	"""make packing list for Product Bundle item"""
	if doc.get("_action") and doc._action == "update_after_submit": return

	parent_items, reset = [], False
	stale_packed_items_table = get_indexed_packed_items_table(doc)

	if not doc.is_new():
		reset = reset_packing_list_if_deleted_items_exist(doc)

	for item in doc.get("items"):
		if frappe.db.exists("Product Bundle", {"new_item_code": item.item_code}):
			for bundle_item in get_product_bundle_items(item.item_code):
				pi_row = add_packed_item_row(
					doc=doc, packing_item=bundle_item,
					main_item_row=item, packed_items_table=stale_packed_items_table,
					reset=reset
				)
				update_packed_item_details(bundle_item, pi_row, item, doc)

			if [item.item_code, item.name] not in parent_items:
				parent_items.append([item.item_code, item.name])

	if frappe.db.get_single_value("Selling Settings", "editable_bundle_item_rates"):
		update_product_bundle_price(doc, parent_items)

def get_indexed_packed_items_table(doc):
	"""
		Create dict from stale packed items table like:
		{(Parent Item 1, Bundle Item 1, ae4b5678): {...}, (key): {value}}
	"""
	indexed_table = {}
	for packed_item in doc.get("packed_items"):
		key = (packed_item.parent_item, packed_item.item_code, packed_item.parent_detail_docname)
		indexed_table[key] = packed_item

	return indexed_table

def reset_packing_list_if_deleted_items_exist(doc):
	doc_before_save = doc.get_doc_before_save()
	reset_table = False

	if doc_before_save:
		# reset table if items were deleted
		reset_table = len(doc_before_save.get("items")) > len(doc.get("items"))
	else:
		reset_table = True # reset if via Update Items (cannot determine action)

	if reset_table:
		doc.set("packed_items", [])
	return reset_table

def get_product_bundle_items(item_code):
	product_bundle = frappe.qb.DocType("Product Bundle")
	product_bundle_item = frappe.qb.DocType("Product Bundle Item")

	query = (
		frappe.qb.from_(product_bundle_item)
		.join(product_bundle).on(product_bundle_item.parent == product_bundle.name)
		.select(
			product_bundle_item.item_code,
			product_bundle_item.qty,
			product_bundle_item.uom,
			product_bundle_item.description
		).where(
			product_bundle.new_item_code == item_code
		).orderby(
			product_bundle_item.idx
		)
	)
	return query.run(as_dict=True)

def add_packed_item_row(doc, packing_item, main_item_row, packed_items_table, reset):
	"""Add and return packed item row.
		doc: Transaction document
		packing_item (dict): Packed Item details
		main_item_row (dict): Items table row corresponding to packed item
		packed_items_table (dict): Packed Items table before save (indexed)
		reset (bool): State if table is reset or preserved as is
	"""
	exists, pi_row = False, {}

	# check if row already exists in packed items table
	key = (main_item_row.item_code, packing_item.item_code, main_item_row.name)
	if packed_items_table.get(key):
		pi_row, exists = packed_items_table.get(key), True

	if not exists:
		pi_row = doc.append('packed_items', {})
	elif reset: # add row if row exists but table is reset
		pi_row.idx, pi_row.name = None, None
		pi_row = doc.append('packed_items', pi_row)

	return pi_row

def get_packed_item_details(item_code, company):
	item = frappe.qb.DocType("Item")
	item_default = frappe.qb.DocType("Item Default")
	query = (
		frappe.qb.from_(item)
		.left_join(item_default)
		.on(
			(item_default.parent == item.name)
			& (item_default.company == company)
		).select(
			item.item_name, item.is_stock_item,
			item.description, item.stock_uom,
			item_default.default_warehouse
		).where(
			item.name == item_code
		)
	)
	return query.run(as_dict=True)[0]

def update_packed_item_details(packing_item, pi_row, main_item_row, doc):
	"Update additional packed item row details."
	item = get_packed_item_details(packing_item.item_code, doc.company)

	prev_doc_packed_items_map = None
	if doc.amended_from:
		prev_doc_packed_items_map = get_cancelled_doc_packed_item_details(doc.packed_items)

	pi_row.parent_item = main_item_row.item_code
	pi_row.parent_detail_docname = main_item_row.name
	pi_row.item_code = packing_item.item_code
	pi_row.item_name = item.item_name
	pi_row.uom = item.stock_uom
	pi_row.qty = flt(packing_item.qty) * flt(main_item_row.stock_qty)
	pi_row.conversion_factor = main_item_row.conversion_factor

	if not pi_row.description:
		pi_row.description = packing_item.get("description")

	if not pi_row.warehouse and not doc.amended_from:
		pi_row.warehouse = (main_item_row.warehouse if ((doc.get('is_pos') or item.is_stock_item \
			or not item.default_warehouse) and main_item_row.warehouse) else item.default_warehouse)

	# TODO batch_no, actual_batch_qty, incoming_rate

	if not pi_row.target_warehouse:
		pi_row.target_warehouse = main_item_row.get("target_warehouse")

	bin = get_packed_item_bin_qty(packing_item.item_code, pi_row.warehouse)
	pi_row.actual_qty = flt(bin.get("actual_qty"))
	pi_row.projected_qty = flt(bin.get("projected_qty"))

	if prev_doc_packed_items_map and prev_doc_packed_items_map.get((packing_item.item_code, main_item_row.item_code)):
		prev_doc_row = prev_doc_packed_items_map.get((packing_item.item_code, main_item_row.item_code))
		pi_row.batch_no = prev_doc_row[0].batch_no
		pi_row.serial_no = prev_doc_row[0].serial_no
		pi_row.warehouse = prev_doc_row[0].warehouse

def get_packed_item_bin_qty(item, warehouse):
	bin_data = frappe.db.get_values(
		"Bin",
		fieldname=["actual_qty", "projected_qty"],
		filters={"item_code": item, "warehouse": warehouse},
		as_dict=True
	)

	return bin_data[0] if bin_data else {}

def get_cancelled_doc_packed_item_details(old_packed_items):
	prev_doc_packed_items_map = {}
	for items in old_packed_items:
		prev_doc_packed_items_map.setdefault((items.item_code ,items.parent_item), []).append(items.as_dict())
	return prev_doc_packed_items_map

def update_product_bundle_price(doc, parent_items):
	"""Updates the prices of Product Bundles based on the rates of the Items in the bundle."""
	if not doc.get('items'):
		return

	parent_items_index = 0
	bundle_price = 0

	for bundle_item in doc.get("packed_items"):
		if parent_items[parent_items_index][0] == bundle_item.parent_item:
			bundle_item_rate = bundle_item.rate if bundle_item.rate else 0
			bundle_price += bundle_item.qty * bundle_item_rate
		else:
			update_parent_item_price(doc, parent_items[parent_items_index][0], bundle_price)

			bundle_item_rate = bundle_item.rate if bundle_item.rate else 0
			bundle_price = bundle_item.qty * bundle_item_rate
			parent_items_index += 1

	# for the last product bundle
	if doc.get("packed_items"):
		update_parent_item_price(doc, parent_items[parent_items_index][0], bundle_price)

def update_parent_item_price(doc, parent_item_code, bundle_price):
	parent_item_doc = doc.get('items', {'item_code': parent_item_code})[0]

	current_parent_item_price = parent_item_doc.amount
	if current_parent_item_price != bundle_price:
		parent_item_doc.amount = bundle_price
		parent_item_doc.rate = bundle_price/(parent_item_doc.qty or 1)


@frappe.whitelist()
def get_items_from_product_bundle(row):
	row, items = json.loads(row), []

	bundled_items = get_product_bundle_items(row["item_code"])
	for item in bundled_items:
		row.update({
			"item_code": item.item_code,
			"qty": flt(row["quantity"]) * flt(item.qty)
		})
		items.append(get_item_details(row))

	return items

def on_doctype_update():
	frappe.db.add_index("Packed Item", ["item_code", "warehouse"])

