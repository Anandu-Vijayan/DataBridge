app_name = "databridge"
app_title = "Frappe DataBridge"
app_publisher = "Faircode Infotech"
app_description = "Universal data migration and synchronization for Frappe applications"
app_email = "anandu@faircodetech.com"
app_license = "mit"

# Frappe only. DataBridge must work without ERPNext.
required_apps = ["frappe"]

# Desk form scripts live next to each DocType (databridge_job.js, databridge_mapping.js).
# Phase 1 ships no SPA, no scheduler events and no doc_events.
