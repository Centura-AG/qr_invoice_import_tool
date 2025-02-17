// Copyright (c) 2025, Centura AG and contributors
// For license information, please see license.txt

frappe.ui.form.on('QR Invoice Import Tool', {
  refresh: function (frm) {
    frm.disable_save();
    setupPrimaryAction(frm);

    if (frm.doc.create_missing_party) {
      frm.set_df_property(
        'party',
        'fieldtype',
        'Data',
        frm.doc.name,
        'invoices'
      );
    }
    const defaultCompany = frappe.defaults.get_default('company');
    if (defaultCompany) {
      frm.set_value('company', defaultCompany).then(() => {
        fetchCompanyDefaults(defaultCompany, frm);
      });
    }
  },

  scan_qr_invoice: function (frm) {
    openQRCodeScanner(frm);
  }
});

function setupPrimaryAction(frm) {
  frm.page.set_primary_action(__('Create Invoices'), () => {
    const primaryButton = frm.page.btn_primary.get(0);

    frm
      .call({
        doc: frm.doc,
        btn: $(primaryButton),
        method: 'make_invoices',
        freeze: true,
        freeze_message: __('Creating Purchase Invoices ...')
      })
      .then((response) => {
        if (response.message) {
          frm.set_value('invoices', []);
          frm.refresh_field('invoices');
          frappe.msgprint(
            __('Purchase Invoices created successfully'),
            __('Success')
          );
        }
      });
  });
}

function openQRCodeScanner(frm) {
  const scanner = new frappe.ui.Scanner({
    dialog: true,
    multiple: false,
    on_scan: (qrData) => handleQRScan(qrData, frm)
  });
}

function handleQRScan(qrData, frm) {
  frappe.call({
    method:
      'qr_invoice_import_tool.qr_invoice_import_tool.doctype.qr_invoice_import_tool.qr_invoice_import_tool.process_qr_data',
    args: {
      qr_data: qrData,
      company: frm.doc.company,
      default_item: frm.doc.default_item,
      default_expense_account: frm.doc.default_expense_account,
      create_missing_supplier: frm.doc.create_missing_supplier
    },
    callback: (response) => {
      if (response?.message) {
        const { invoice_details } = response.message;
        const newRow = frm.add_child('invoices');

        Object.entries(invoice_details).forEach(([fieldname, value]) => {
          frappe.model.set_value(newRow.doctype, newRow.name, fieldname, value);
        });

        frm.refresh_field('invoices');
        frappe.msgprint(__('QR Invoice successfully imported'), __('Success'));
      }
    }
  });
}

function fetchCompanyDefaults(company, frm) {
  frappe.db.get_value(
    'Company',
    company,
    ['default_expense_account', 'default_income_account'],
    (values) => {
      if (values) {
        frm.set_value(
          'default_expense_account',
          values.default_expense_account
        );
      }
    }
  );
}
