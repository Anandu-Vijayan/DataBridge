### DataBridge

Migrate data from files and databases into **any** Frappe DocType. Independent app: it does not need ERPNext.

MIT licensed, by Faircode Infotech.

#### What Phase 1 does

- Sources: CSV, TSV, XLSX, JSON (array of objects), JSONL, XML, MySQL/MariaDB.
- Field mapping with transforms (`trim`, `upper`, `lower`, `title`, `int`, `float`, `bool`, `date`, `datetime`, `replace`, `null_if_empty`, `strip_html`) and restricted formulas.
- Validation against the target DocType (types, select options, links, lengths, required fields).
- Write modes: Insert, Update, Upsert, Skip. Dry run. Optional submit.
- Batches with checkpoints, pause, resume, cancel, and retry of failed records only.
- Per-record results and an execution log. Everything runs from Desk form buttons.

Not in Phase 1: child-table mapping, job dependencies, sync and watermarks, reconciliation reports, REST/SOAP, Python transform plugins, a SPA, Postgres/MSSQL/SQLite sources. The data model reserves room for them.

#### Install

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO
bench --site your.site install-app databridge
```

#### Use

1. **DataBridge Connection**: pick the source. For files, upload a *private* file. For MySQL, fill host, database, user and password. Press **Test Connection**.
2. **DataBridge Mapping**: choose connection, source object, target DocType and the source column that is the stable id. **Suggest Mapping** fills exact name matches. **Validate Mapping** lists every problem.
3. **DataBridge Job**: choose the mapping, mode and batch size. Press **Dry Run** first, read the failed records, then **Create Real Run**.
4. Pause, Resume, Cancel and **Retry Failed Records** are on the job form.

#### How it stays safe

- Streams everything in batches. One background job processes one batch, commits, then queues the next (queue `long`).
- Formulas use `frappe.safe_eval` with only `value` and `row`.
- MySQL: read-only session, identifiers checked against `information_schema` then quoted, values always bound.
- Passwords live in Password fields and are never returned, logged or staged.
- Targets are written only through `get_doc().insert()/save()/submit()` as the user who started the job, with that user's permissions. Permissions are re-checked on every batch.
- Each target document and its Migration Reference are written in one transaction, and the reference is checked first, so a retry or a re-run cannot create duplicates.
- A job uses the mapping snapshot taken at start. Editing the mapping does not change a running job.
- XML is parsed with defusedxml only.

#### Known limits (read before relying on it)

- JSON arrays and XML resume by re-parsing, without keeping, the records before the checkpoint. Use JSONL for very large files.
- A CSV row with more columns than the header drops the extra values. Shorter rows are padded with empty values.
- CSV/TSV must be UTF-8 or another ASCII-compatible encoding. UTF-16/32 is rejected.
- MySQL with SSL encrypts the link but verifies the certificate only when `ssl_ca` is set in Extra Options.
- Staged source data is kept only for failed records (needed for retry), with the connection password redacted.
- Source IDs: a record without one fails with `MISSING_SOURCE_ID`.

#### Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install ruff pytest defusedxml openpyxl pymysql
ruff check databridge && ruff format --check databridge
pytest                                   # pure unit tests, no site needed
bench --site <site> run-tests --app databridge   # integration tests
```

MySQL integration tests are skipped unless `DATABRIDGE_TEST_MYSQL_HOST/USER/PASSWORD/DB` are set in the environment.

Test base classes are imported only from `databridge/tests/base.py`, so moving from `FrappeTestCase` (v15) to `IntegrationTestCase` (v16) is a one-file change.

#### License

MIT
