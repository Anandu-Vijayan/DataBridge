// Copyright (c) 2026, Faircode Infotech and contributors
// For license information, please see license.txt

frappe.ui.form.on("DataBridge Mapping", {
	refresh(frm) {
		databridge_mapping.load_pickers(frm);
		if (frm.is_new()) {
			return;
		}
		frm.add_custom_button(__("Suggest Mapping"), () => databridge_mapping.suggest(frm));
		frm.add_custom_button(__("Validate Mapping"), () => databridge_mapping.validate(frm));
	},

	target_doctype(frm) {
		databridge_mapping.load_pickers(frm);
	},
	connection(frm) {
		databridge_mapping.load_pickers(frm);
	},
	source_object(frm) {
		databridge_mapping.load_pickers(frm);
	},
});

const databridge_mapping = {
	get(method, args) {
		return frappe.call({ method: "databridge.api.mapping." + method, args }).then((r) => r.message || {});
	},

	fail(res) {
		const err = res.error || {};
		frappe.msgprint({
			title: __("Mapping problem"),
			message: `${frappe.utils.escape_html(err.code || "")}: ${frappe.utils.escape_html(err.message || "")}`,
			indicator: "red",
		});
	},

	set_options(frm, fieldname, names) {
		const grid = frm.fields_dict.fields && frm.fields_dict.fields.grid;
		if (!grid || !names.length) {
			return;
		}
		grid.update_docfield_property(fieldname, "fieldtype", "Autocomplete");
		grid.update_docfield_property(fieldname, "options", names);
	},

	load_pickers(frm) {
		if (frm.doc.target_doctype) {
			databridge_mapping.get("get_target_fields", { doctype: frm.doc.target_doctype }).then((res) => {
				if (res.ok) {
					databridge_mapping.set_options(frm, "target_field", res.data.map((f) => f.fieldname));
				}
			});
		}
		if (frm.doc.connection && frm.doc.source_object) {
			frappe
				.call({
					method: "databridge.api.connection.list_source_fields",
					args: { connection: frm.doc.connection, source_object: frm.doc.source_object },
				})
				.then((r) => {
					const res = r.message || {};
					if (res.ok) {
						databridge_mapping.set_options(frm, "source_field", res.data);
					}
				});
		}
	},

	suggest(frm) {
		if (frm.is_dirty()) {
			frappe.msgprint(__("Save the mapping first."));
			return;
		}
		databridge_mapping
			.get("suggest_mapping", {
				connection: frm.doc.connection,
				source_object: frm.doc.source_object,
				target_doctype: frm.doc.target_doctype,
			})
			.then((res) => {
				if (!res.ok) {
					return databridge_mapping.fail(res);
				}
				if (!res.data.length) {
					frappe.msgprint(__("No exact name matches found. Map the fields by hand."));
					return;
				}
				const apply = () => {
					frm.clear_table("fields");
					res.data.forEach((row) => frm.add_child("fields", row));
					frm.refresh_field("fields");
					frm.dirty();
				};
				if ((frm.doc.fields || []).length) {
					frappe.confirm(__("Replace the {0} existing rows with {1} suggestions?", [frm.doc.fields.length, res.data.length]), apply);
				} else {
					apply();
				}
			});
	},

	validate(frm) {
		if (frm.is_dirty()) {
			frappe.msgprint(__("Save the mapping first."));
			return;
		}
		databridge_mapping.get("validate_mapping", { mapping: frm.doc.name }).then((res) => {
			if (!res.ok) {
				return databridge_mapping.fail(res);
			}
			if (res.data.valid) {
				frappe.show_alert({ message: __("Mapping is valid"), indicator: "green" });
			} else {
				frappe.msgprint({
					title: __("Mapping problems"),
					message: res.data.problems.map((p) => frappe.utils.escape_html(p)).join("<br>"),
					indicator: "orange",
				});
			}
		});
	},
};
