# Deploying on Vercel (no Render, no external backend)

The project is one Vercel project: static page from `public/`, API as one Python (Flask) Vercel Function. The frontend calls relative paths (`/api/funds`, ...), so there is no backend URL to configure.

## Steps
1. Create a new GitHub repository and upload the contents of this folder (the folder itself is the repository root: `app.py`, `vercel.json`, `requirements.txt`, `backend/`, `public/`, `data/` must be at the top level).
2. In Vercel: Add New > Project > import the repository.
3. Leave every setting at its default (below) and click Deploy.
4. Open `https://<your-project>.vercel.app/api/health`. The first call builds the model (a few seconds) and should show `"ready": true`, `"n_funds": 12`, `"source": "bundled_dataset"`.

## Exact project settings
| Setting | Value |
|---|---|
| Framework Preset | **Flask** (or leave "Other": `vercel.json` sets `"framework": "flask"`, which overrides the dashboard preset; verified with Vercel's CLI build for both) |
| Root Directory | repository root (blank) |
| Build Command | leave empty |
| Output Directory | leave empty (`public/` is served statically) |
| Install Command | leave empty (installs `requirements.txt`) |
| Python version | 3.13, taken from `.python-version` (Vercel supports 3.12, 3.13, 3.14) |
| Function duration | `maxDuration` 120 s, set in `vercel.json` (Hobby and Pro allow up to 300 s with Fluid compute) |
| Function memory | default (2 GB on Hobby) |
| Environment variables | **none required** |

Files that make this work: `app.py` (entrypoint exporting `app`), `backend/` (the Python modules), `data/` (raw, cleaned, provenance, quality report), `public/index.html`, `requirements.txt`, `.python-version`, `vercel.json` (`"framework": "flask"`, duration, `includeFiles` for `data/**` and `public/**`, and excludes `tests/`, `tools/`, `docs/`, `results/` from the function bundle; `data/` is NOT excluded).
`requirements.txt` pins `numpy==2.5.3`, `pandas==3.0.5`, `scipy==1.18.1` and `flask`. `mftool`, `requests` and `flask-cors` were removed: nothing is fetched from the network.

## How the app behaves in serverless
* On the first API request of an instance the model is built from the bundled files, in memory only (about 7 s measured in the bundle emulation, see `results/deployment_verification.json`; the function limit in `vercel.json` is 120 s). Concurrent first requests wait for the same build. A warm instance reuses it. A new cold instance rebuilds it; results are identical because data and seeds are fixed.
* Nothing is written to disk; no background threads; no live NAV provider.
* Successful `GET /api/*` responses (not `/api/health`, not errors) carry `Cache-Control: s-maxage=600`, so the CDN can answer repeat requests without waking the function. Each new deployment starts with a fresh cache.
* `/api/health?light=1` answers without building the model; `/api/health` builds it and reports readiness, build time, dataset status, Python version and whether the data folder is present.

## G-Sec index levels (included)
`data/raw/Nifty GS Compsite Historical Data.csv` (Investing.com export, 2016-04-05 to 2024-12-31) is bundled and ingested (`data/cleaned/debt.csv`, checksum in `data/provenance.json`). It is not confirmed to be the NSE Total Return Index. To replace it with the official series: on your computer run `python tools/fetch_gsec_nse.py` (needs internet; untested against the live endpoint) or place a date/level CSV in `data/raw/`, then `python tools/ingest_raw.py --raw data/raw --debt-file <file> --retrieved-on YYYY-MM-DD`, then `python tools/generate_results.py`, commit and push; Vercel redeploys.

## About the `unused_function` build error (fixed)
Error: `The pattern "app.py" defined in functions doesn't match any Serverless Functions inside the api directory.`
Cause: with Framework Preset "Other" no framework is detected, so Vercel uses its classic mode where `functions` patterns are matched only against files in `api/`. The root `app.py` is only built as a function when the Flask framework is active. Fix: `"framework": "flask"` in `vercel.json`. The `functions` entry keyed `app.py` (the resolved entrypoint, as in Vercel's Flask documentation) then matches, and `maxDuration` 120 and `excludeFiles` apply. Do not remove the `functions` block: it carries the 120 s duration and the exclusions.

## Data and frontend not found on a deployment (second fix)
Symptoms: `/` returns plain `Not found`; `/api/funds` says the bundled NAV dataset was not found. Both are the function not seeing `data/` and `public/` at run time (the root page is also served by Flask as a fallback when the CDN does not answer `/`). Changes: `vercel.json` now lists `"includeFiles": "{data/**,public/**}"`; the code looks for `data/` next to `backend/` and, if absent, in the working directory (Vercel documents the working directory as the project base); `/api/health?light=1&diag=1` lists what the running function can see (working directory, data and public paths, each dataset file with size, directory listings); the NAV error message names the path it searched.
The cause on your deployment could not be reproduced here: in Vercel's own `vercel build` output the files were already bundled, so this is a hardening plus a diagnostic, not a confirmed root-cause fix. If it persists after redeploying, open `https://<project>.vercel.app/api/health?light=1&diag=1` and send me the JSON. Also check on GitHub that `data/cleaned/fund_nav.csv` and `public/index.html` really exist in the repository branch Vercel deploys, and that the Project Settings Root Directory is blank.

## If something does not work after deploying
* `/api/health` returns 503 or `ready: false`: read `error` in the response and `deployment.data_dir_exists`. A false value means `data/` was not uploaded or was excluded.
* Build fails on bundle size: set `VERCEL_ANALYZE_BUILD_OUTPUT=1` and redeploy to see sizes. The measured dependency size is about 215 MB, below the 500 MB Python limit documented by Vercel.
* The page loads but API calls fail with 404: check that `app.py` is at the repository root and that no Project Setting (Build Command, Output Directory, Root Directory) was overridden. Do not add an `api/` folder, `builds` or `routes` to `vercel.json`.

## What was and was not verified
Local verification with Vercel's own CLI (`vercel build`, no account), `python tools/verify_vercel_build.py` (set `E2E_BROWSER=1` and `E2E_CHARTJS=<local chart.umd.js>` for the browser step, because the sandbox has no internet): the build succeeds with dashboard preset "Other" and "Flask"; the built function lists `app.py`, `backend/`, all of `data/` (five cleaned datasets, provenance, quality report) and `public/index.html`, with `maxDuration` 120 and no `tests/`, `tools/`, `docs/`, `results/`; the bundle was assembled exactly as listed in the build output and run behind a small router that follows the built routing order (static output first, then the function); `/` serves the frontend, `/api/health` has ready=true and n_funds=12, `/api/funds` returns 12, all other GET routes return 200, and headless Chromium loads the page, calls the API with no failed request and no JavaScript error. The same file check confirms `data/cleaned/fund_nav.csv` exists in the bundle at the path the application resolves. The full test suite passes (148).
NOT verified (needs the live site): Vercel's CDN routing of `/`, the live function's file layout, cold-start time and memory on Vercel hardware. The router above is my emulation of the documented order, not Vercel's CDN. The page also loads Chart.js from cdnjs in the visitor's browser.
