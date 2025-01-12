# Copyright (c) 2025, Centura AG and contributors
# For license information, please see license.txt

import frappe
import json
from datetime import date
from frappe import _, scrub
from frappe.model.document import Document
from frappe.utils import flt, nowdate
from frappe.utils.background_jobs import enqueue, is_job_enqueued
from erpnext.accounts.doctype.accounting_dimension.accounting_dimension import get_accounting_dimensions

class QRInvoiceImportTool(Document):
    
    @frappe.whitelist()
    def make_invoices(self):
        self.validate_company()
        invoices = self.get_invoices()

        if len(invoices) < 50:
            return start_import(invoices)
        
        if frappe.utils.scheduler.is_scheduler_inactive() and not frappe.flags.in_test:
            frappe.throw(_("Scheduler is inactive. Cannot import data."), title=_("Scheduler Inactive"))

        job_id = f"qr_invoice::{self.name}"
        
        if not is_job_enqueued(job_id):
            enqueue(
                start_import,
                queue="default",
                timeout=6000,
                event="qr_invoice_creation",
                job_id=job_id,
                invoices=invoices,
                now=frappe.conf.developer_mode or frappe.flags.in_test,
            )

    def get_invoice_dict(self, row):
        def get_item_dict():
            cost_center = row.get("cost_center") or frappe.get_cached_value("Company", self.company, "cost_center")
            
            if not cost_center:
                frappe.throw(_("Please set the Default Cost Center in {0} company.").format(frappe.bold(self.company)))

            income_expense_account_field = "income_account" if row.party_type == "Customer" else "expense_account"
            default_uom = frappe.db.get_single_value("Stock Settings", "stock_uom") or _("")
            rate = flt(row.outstanding_amount) / flt(row.qty)

            item_dict = {
                "uom": default_uom,
                "rate": rate or 0.0,
                "qty": row.qty,
                "conversion_factor": 1.0,
                "item_name": row.item,
                "description": row.item,
                income_expense_account_field: row.account,
                "cost_center": cost_center,
            }

            for dimension in get_accounting_dimensions():
                item_dict[dimension] = row.get(dimension)

            return item_dict

        item = get_item_dict()

        invoice = {
            "items": [item],
            "is_opening": "No",
            "set_posting_time": 1,
            "company": self.company,
            "cost_center": self.cost_center,
            "due_date": row.due_date,
            "posting_date": row.posting_date,
            scrub(row.party_type): row.party,
            "is_pos": 0,
            "doctype": "Purchase Invoice",
            "update_stock": 0,
            "invoice_number": row.reference_number,
            "disable_rounded_total": 1,
        }

        for dimension in get_accounting_dimensions():
            invoice[dimension] = self.get(dimension) or row.get(dimension)

        return invoice

    def set_missing_values(self, row):
        row.qty = row.qty or 1.0
        row.party_type = "Supplier"
        row.item = row.item or _("")
        row.posting_date = row.posting_date or nowdate()
        row.due_date = row.due_date or nowdate()

    def validate_mandatory_invoice_fields(self, row):
        if not frappe.db.exists(row.party_type, row.party):
            if self.create_missing_party:
                self.add_party(row.party_type, row.party)
            else:
                frappe.throw(_("Row #{}: {} {} does not exist.").format(
                    row.idx, frappe.bold(row.party_type), frappe.bold(row.party)
                ))

        mandatory_error_msg = _("Row #{0}: {1} is required to create the {2} Invoices")
        for field in ("Party", "Outstanding Amount", "Account"):
            if not row.get(scrub(field)):
                frappe.throw(mandatory_error_msg.format(row.idx, field, self.invoice_type))

    def validate_company(self):
        if not self.company:
            frappe.throw(_("Please select the Company"))

    def get_invoices(self):
        invoices = []
        for row in self.invoices:
            if not row:
                continue
            self.set_missing_values(row)
            self.validate_mandatory_invoice_fields(row)
            invoice = self.get_invoice_dict(row)

            company_details = frappe.get_cached_value(
                "Company", self.company, ["default_currency", "default_letter_head"], as_dict=True
            ) or {}

            default_currency = frappe.db.get_value(row.party_type, row.party, "default_currency")

            if company_details:
                invoice.update({
                    "currency": default_currency or company_details.get("default_currency"),
                    "letter_head": company_details.get("default_letter_head"),
                })
            invoices.append(invoice)

        return invoices


def start_import(invoices):
    errors = 0
    names = []

    for idx, invoice_data in enumerate(invoices):
        try:
            doc = frappe.get_doc(invoice_data)
            doc.flags.ignore_mandatory = True
            doc.insert(set_name=invoice_data.get("invoice_number"))
            doc.save()
            frappe.db.commit()
            names.append(doc.name)
        except Exception:
            errors += 1
            frappe.db.rollback()
            frappe.log_error(frappe.get_traceback(), "QR invoice creation failed")

    if errors:
        frappe.msgprint(_(
            "You had {} errors while creating QR invoices. Check {} for more details"
        ).format(errors, "<a href='/app/List/Error Log' class='variant-click'>Error Log</a>"),
        indicator="red", title=_("Error Occured"))
    return names


@frappe.whitelist()
def process_qr_data(qr_data, company, default_item, default_expense_account, create_missing_supplier):
    try:
        create_missing_supplier = frappe.utils.cint(create_missing_supplier)
        invoice_detail = parse_qr_invoice_data(company, qr_data, default_item, create_missing_supplier, default_expense_account)
        return {"invoice_details": invoice_detail} 
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), _("QR Data Processing Error"))
        frappe.throw(_("Error processing QR data: {0}").format(str(e)))

def parse_qr_invoice_data(company, qr_data, default_item, create_missing_supplier, account):
    qr_data = json.loads(qr_data)
    qr_content = qr_data.get("decodedText", "")
    lines = qr_content.split("\n")

    party_data = {
        "party_type": "Supplier",
        "party_name": lines[5],
        "address": {
            "address_line1": lines[6] + lines[7],
            "city": lines[9],
            "pincode": lines[8],
            "country": frappe.db.get_value("Country", {"code": lines[10].lower()}, "name")
        }
    }
    
    invoice_details = {
        "company": company,
        "reference_number": lines[28],
        "party_type": "Supplier",
        "party": match_party(party_data, create_missing_supplier),
        "posting_date": date.today().isoformat(),
        "item": default_item,
        "account": account,
        "outstanding_amount": lines[18],
        "description": lines[29],
        "qty": "1"
    }

    return invoice_details


def match_party(party_data, create_missing_supplier):
    party_name = party_data.get("party_name")
    party = frappe.db.get_list("Supplier", filters={"supplier_name": party_name})

    if not party and create_missing_supplier:

        supplier = create_supplier(party_data)
        if not supplier:
            frappe.throw(_("Error creating supplier: {0}").format(party_name))
        create_address(party_data, supplier)

        return supplier

    elif not party:
        frappe.throw(_("Supplier not found: {0}").format(party_name))

    if len(party) > 1:
        frappe.throw(_("Multiple suppliers found with the same name: {0}").format(party_name))

    return party[0].name

def create_supplier(data):
    try:
        supplier = frappe.new_doc("Supplier")
        supplier.supplier_name = data.get("party_name")
        supplier.insert(ignore_permissions=True)
        frappe.db.commit()
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), _("Supplier Creation Error"))
        return None
    return supplier.name

def create_address(data, party):
    try:
        address = frappe.new_doc("Address")
        address.update({
            "address_type": "Billing",
            "address_line1": data.get("address").get("address_line1"),
            "city": data.get("address").get("city"),
            "country": data.get("address").get("country"),
            "pincode": data.get("address").get("pincode"),
            "is_primary_address": 1,
            "links": [{"link_doctype": data.get("party_type"), "link_name": party}]
        })
        address.insert(ignore_permissions=True)
        frappe.db.commit()
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), _("Address Creation Error"))
        return None
    return address.name
