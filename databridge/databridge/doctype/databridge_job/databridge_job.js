// Copyright (c) 2026, Faircode Infotech and contributors
// For license information, please see license.txt

const DATABRIDGE_API = "databridge.api.run.";
const ACTIVE = ["Queued", "Running"];

frappe.ui.form.on("DataBridge Job", {
	refresh(frm) {
		if (frm.is_new()) {
			return;
		}
		databridge_job.add_buttons(frm);
		databridge_job.show_progress(frm);
		databridge_job.watch(frm);
	},

	onload(frm) {
		frappe.realtime.on("databridge_job_update", (data) => {
			if (data.job === frm.doc.name) {
				frm.reload_doc();
			}
		});
	},

	before_unload(frm) {
		databridge_job.stop_polling(frm);
	},
});

const databridge_job = {
	call(frm, method, args, success_message) {
		if (frm.is_dirty()) {
			frappe.msgprint(__("Save the job first."));
			return;
		}
		frappe.call({
			method: DATABRIDGE_API + method,
			type: "POST",
			args: Object.assign({ job: frm.doc.name }, args || {}),
			freeze: true,
			callback(r) {
				const res = r.message || {};
				if (!res.ok) {
					const err = res.error || {};
					frappe.msgprint({
						title: __("Could not complete the action"),
						message: `${frappe.utils.escape_html(err.code || "")}: ${frappe.utils.escape_html(err.message || "")}`,
						indicator: "red",
					});
					return;
				}
				if (success_message) {
					frappe.show_alert({ message: success_message, indicator: "green" });
				}
				if (method === "create_real_run") {
					frappe.set_route("Form", "DataBridge Job", res.data.job);
					return;
				}
				frm.reload_doc();
			},
		});
	},

	add_buttons(frm) {
		const status = frm.doc.status;
		const group = __("Actions");
		const add = (label, handler, primary) => {
			frm.add_custom_button(label, handler, group);
			if (primary) {
				frm.page.set_inner_btn_group_as_primary(group);
			}
		};

		if (status === "Draft") {
			add(__("Dry Run"), () => databridge_job.call(frm, "start_job", { dry_run: 1 }, __("Dry run queued")));
			add(
				__("Start Migration"),
				() => {
					frappe.confirm(__("Write data to the target DocType now?"), () =>
						databridge_job.call(frm, "start_job", { dry_run: 0 }, __("Migration queued")),
					);
				},
				true,
			);
		}
		if (ACTIVE.includes(status) && !frm.doc.stop_requested) {
			add(__("Pause"), () => databridge_job.call(frm, "pause_job", {}, __("Pause requested")));
		}
		if (["Paused", "Failed"].includes(status)) {
			add(__("Resume"), () => databridge_job.call(frm, "resume_job", {}, __("Resumed")), true);
		}
		if (ACTIVE.includes(status) || status === "Paused") {
			if (frm.doc.stop_requested !== "Cancel") {
				add(__("Cancel"), () => {
					frappe.confirm(__("Cancel this job? Records already written stay in place."), () =>
						databridge_job.call(frm, "cancel_job", {}, __("Cancel requested")),
					);
				});
			}
		}
		if (["Completed With Errors", "Failed"].includes(status) && !frm.doc.dry_run && frm.doc.failed > 0) {
			add(__("Retry Failed Records"), () => databridge_job.call(frm, "retry_failed", {}, __("Retry queued")), true);
		}
		if (frm.doc.dry_run && ["Completed", "Completed With Errors"].includes(status)) {
			add(__("Create Real Run"), () => databridge_job.call(frm, "create_real_run"), true);
		}
		add(__("Failed Records"), () =>
			frappe.set_route("List", "DataBridge Job Record", { job: frm.doc.name, status: "Failed" }),
		);
		add(__("All Records"), () => frappe.set_route("List", "DataBridge Job Record", { job: frm.doc.name }));
		add(__("Execution Log"), () => frappe.set_route("List", "DataBridge Execution Log", { job: frm.doc.name }));
	},

	show_progress(frm) {
		const d = frm.doc;
		const parts = [
			__("Read {0}", [d.total_read || 0]),
			__("Inserted {0}", [d.inserted || 0]),
			__("Updated {0}", [d.updated || 0]),
			__("Skipped {0}", [d.skipped || 0]),
			__("Failed {0}", [d.failed || 0]),
		];
		if (d.dry_run) {
			parts.push(__("Validated {0}", [d.validated || 0]));
		}
		const colour = d.failed > 0 ? "orange" : ACTIVE.includes(d.status) ? "blue" : "green";
		frm.dashboard.set_headline(
			`<span class="indicator-pill ${colour}">${frappe.utils.escape_html(d.status)}</span> ` +
				frappe.utils.escape_html(parts.join("  |  ")) +
				(d.stop_requested ? ` (${frappe.utils.escape_html(d.stop_requested)} requested)` : ""),
		);
	},

	watch(frm) {
		databridge_job.stop_polling(frm);
		if (ACTIVE.includes(frm.doc.status)) {
			frm._databridge_poll = setInterval(() => {
				if (frm.is_dirty()) {
					return;
				}
				frm.reload_doc();
			}, 5000);
		}
	},

	stop_polling(frm) {
		if (frm._databridge_poll) {
			clearInterval(frm._databridge_poll);
			frm._databridge_poll = null;
		}
	},
};
