// Copyright (c) 2026, Faircode Infotech and contributors
// For license information, please see license.txt

frappe.ui.form.on("DataBridge Connection", {
	refresh(frm) {
		if (frm.is_new()) {
			return;
		}
		frm.add_custom_button(__("Test Connection"), () => databridge_connection.test(frm));
		frm.add_custom_button(__("Preview Data"), () => databridge_connection.preview(frm));
	},
});

const databridge_connection = {
	api(method, args, type) {
		return frappe.call({ method: "databridge.api.connection." + method, type: type || "GET", args, freeze: true });
	},

	fail(res) {
		const err = res.error || {};
		frappe.msgprint({
			title: __("Source problem"),
			message: `${frappe.utils.escape_html(err.code || "")}: ${frappe.utils.escape_html(err.message || "")}`,
			indicator: "red",
		});
	},

	test(frm) {
		if (frm.is_dirty()) {
			frappe.msgprint(__("Save the connection first."));
			return;
		}
		databridge_connection.api("test_connection", { connection: frm.doc.name }, "POST").then((r) => {
			const res = r.message || {};
			if (res.ok) {
				frappe.show_alert({ message: __("Connection works"), indicator: "green" });
			} else {
				databridge_connection.fail(res);
			}
			frm.reload_doc();
		});
	},

	preview(frm) {
		if (frm.is_dirty()) {
			frappe.msgprint(__("Save the connection first."));
			return;
		}
		databridge_connection.api("list_source_objects", { connection: frm.doc.name }).then((r) => {
			const res = r.message || {};
			if (!res.ok) {
				return databridge_connection.fail(res);
			}
			const objects = res.data || [];
			const dialog = new frappe.ui.Dialog({
				title: __("Preview source data"),
				fields: [
					{
						fieldname: "source_object",
						fieldtype: "Select",
						label: __("Source object"),
						options: objects.join("\n"),
						default: objects[0],
						reqd: 1,
					},
					{ fieldname: "output", fieldtype: "HTML" },
				],
				primary_action_label: __("Load"),
				primary_action(values) {
					databridge_connection
						.api("preview_source", { connection: frm.doc.name, source_object: values.source_object, limit: 10 })
						.then((p) => {
							const out = p.message || {};
							if (!out.ok) {
								return databridge_connection.fail(out);
							}
							databridge_connection.render_table(dialog, out.data);
						});
				},
			});
			dialog.show();
		});
	},

	render_table(dialog, data) {
		const esc = frappe.utils.escape_html;
		const head = data.fields.map((f) => `<th>${esc(f)}</th>`).join("");
		const body = data.rows
			.map((row) => "<tr>" + data.fields.map((f) => `<td>${esc(String(row[f] ?? ""))}</td>`).join("") + "</tr>")
			.join("");
		dialog.fields_dict.output.$wrapper.html(
			`<div style="overflow:auto;max-height:50vh"><table class="table table-bordered"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`,
		);
	},
};
