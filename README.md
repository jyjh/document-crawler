# Document Crawler

A small Python tool for unattended PDF discovery and upload. It recursively scans configured folders, hashes each PDF, asks a configurable HTTP API whether that hash already exists, and uploads only missing files.

The API contract is fully config-driven: URL templates, methods, request body shape, and response detection all live in `config.yaml`.

## Install

Requirements:

- Python 3.10 or newer
- Windows for the included Task Scheduler helper

```powershell
python -m pip install -r requirements.txt
Copy-Item config.example.yaml config.yaml
```

Edit `config.yaml`, then run:

```powershell
python -m doc_crawler --config config.yaml --dry-run --limit 5
```

Run for real:

```powershell
python -m doc_crawler --config config.yaml
```

Useful options:

- `--dry-run`: hash files and log intended check/upload actions without calling the API.
- `--limit N`: process at most `N` discovered files.
- `--no-cache`: ignore the local SQLite hash cache for this run.

## Config

Start from `config.example.yaml`. Relative paths for logs, cache, and CA bundles are resolved relative to the config file, not the current working directory. This avoids surprises when Windows Task Scheduler starts the process from another location.

### Discovery

`crawl.directories` lists explicit folders to scan. As an alternative (or in addition), `crawl.directory_search` walks a parent directory and scans any subdirectory whose name matches a regex pattern:

```yaml
crawl:
  directories:
    - "D:\\Documents\\PDFs"
  directory_search:
    parent: "D:\\Documents\\Projects"
    pattern: "case-\\d+"   # any Python re pattern; matched with re.search
    recursive: true        # walk parent recursively to find matching folders (default true)
```

Each matched folder is then scanned using the crawl's normal `extensions`, `recursive`, `exclude_dirs`, and `max_file_size_mb` settings. At least one of `directories` or `directory_search` is required.

Load-time expansion:

- `${ENV}` is replaced from the environment. Missing variables are config errors. Empty variables warn.
- `file:C:\path\secret.txt` is replaced with the file contents.

Runtime placeholders:

- `{base_url}`
- `{hash}`
- `{filename}`
- `{filepath}`
- `{filesize}`
- `{mtime_iso}`

Unknown placeholders are config errors.

## Server Examples

REST-style `200` exists, `404` missing:

```yaml
check:
  method: GET
  url: "{base_url}/api/documents/exists"
  query: { hash: "{hash}" }
  detect:
    mode: status_map
    status_map: { exists: [200], missing: [404] }
```

JSON boolean response:

```yaml
check:
  method: POST
  url: "{base_url}/api/documents/exists"
  json_body: { hash: "{hash}" }
  detect:
    mode: json_path
    json_path: "exists"
    json_path_truthy: true
```

Nested custom value:

```yaml
check:
  method: GET
  url: "{base_url}/api/documents/status"
  query: { hash: "{hash}" }
  detect:
    mode: json_path
    json_path: "data.state"
    json_path_eq: "present"
```

Multipart upload:

```yaml
upload:
  method: POST
  url: "{base_url}/api/documents"
  format: multipart
  file_field: "file"
  filename_template: "{filename}"
  extra_fields: { hash: "{hash}", filename: "{filename}" }
  detect: { mode: status_in, status_in: [200, 201, 202] }
  idempotent: false
```

`upload.idempotent: false` avoids retrying connection drops while sending the file because the server may have committed a partial or complete upload.

## Exit Codes

- `0`: all processed, no errors
- `1`: completed, but one or more files errored
- `2`: config error
- `3`: server unreachable or fatal runtime policy failure
- `4`: unexpected exception

Per-file errors are logged and the run continues where possible. If `server_unreachable: fail` is set, connection failures during the check phase stop the run with exit code `3`.

## Windows Task Scheduler

Register the task:

```powershell
.\register_task.ps1 `
  -TaskName "DocumentCrawler" `
  -ConfigPath "C:\path\to\document-crawler\config.yaml" `
  -At "02:00" `
  -User "DOMAIN\crawler-user"
```

The registered task uses:

- `python` to run without depending on a virtual environment
- the repo directory as the working directory
- `MultipleInstances IgnoreNew` to prevent overlapping runs
- `StartWhenAvailable` to catch missed schedules
- a two-hour execution limit
- limited run level

Use `run.bat` for ad-hoc local runs from the repo directory. It forwards arguments to `python -m doc_crawler`.

## Logging And Cache

Logs rotate at 5 MB with five backups. Each run gets a short run label in every log line.

The SQLite cache stores `path`, `mtime`, `size`, `hash`, and `algo`. It avoids re-hashing unchanged files when `cache.trust_size_mtime` is true. If the database cannot be opened, the crawler logs a warning and continues with caching disabled.

## Troubleshooting

- Config exits with code `2`: run with `--config` pointing at the intended file and check missing environment variables, invalid placeholders, or `check.query` plus `check.json_body` both being set.
- Server-down exits with code `3`: verify `server.base_url`, network access, TLS settings, and corporate CA bundle settings.
- Files are skipped as locked: close the PDF or let the next scheduled run pick it up.
- TLS fails behind a corporate proxy: prefer `server.ca_bundle` over `verify_tls: false`. Disabling TLS verification should be test-only.

## Development

Run tests:

```powershell
python -m unittest discover -s tests
```

Deferred out of v1:

- PyInstaller `.exe` packaging
- upload progress bars
- RFC 5987 unicode multipart filename handling
- concurrency
- JSONPath wildcards and arrays

