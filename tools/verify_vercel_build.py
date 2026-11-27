"""Run Vercel's own build locally (no account, no deployment) and emulate the built function.
    python tools/verify_vercel_build.py [--framework-setting null|flask|fastapi]
Needs Node/npm, internet for npm and PyPI, and Python 3.13 with the project's requirements.txt installed (for the emulation).
It copies the project to a temp dir, fakes the dashboard project settings (default: framework "Other"), runs
`vercel build`, checks vercel.json was accepted, maxDuration, excluded folders, bundled data and the static page, then
calls every GET route of the built bundle. It does NOT test a live Vercel deployment."""
import argparse, json, os, shutil, subprocess, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--framework-setting", default="null", help="dashboard Framework Preset to simulate: null (Other) | flask")
    a = ap.parse_args()
    tmp = tempfile.mkdtemp(prefix="vcl_build_")
    proj = os.path.join(tmp, "p")
    shutil.copytree(ROOT, proj, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".vercel", ".venv"))
    os.makedirs(os.path.join(proj, ".vercel"))
    fw = "null" if a.framework_setting == "null" else json.dumps(a.framework_setting)
    open(os.path.join(proj, ".vercel", "project.json"), "w").write(
        '{"projectId":"prj_local","orgId":"team_local","settings":{"framework":%s,"rootDirectory":null}}' % fw)
    r = subprocess.run(["npx", "--yes", "vercel@latest", "build", "--yes"], cwd=proj, capture_output=True, text=True)
    print(r.stdout[-1500:], r.stderr[-1500:])
    if r.returncode != 0:
        sys.exit("vercel build FAILED")
    out = os.path.join(proj, ".vercel", "output")
    fdirs = [d for d in os.listdir(os.path.join(out, "functions")) if d.endswith(".func")]
    assert len(fdirs) == 1, fdirs
    fdir = os.path.join(out, "functions", fdirs[0])
    cfg = json.load(open(os.path.join(fdir, ".vc-config.json")))
    fmap = cfg["filePathMap"]
    assert cfg["maxDuration"] == 120, cfg["maxDuration"]
    assert not [k for k in fmap if k.startswith(("tests/", "tools/", "docs/", "results/"))], "excluded folders leaked"
    need = ["app.py", "backend/webapp.py", "data/provenance.json", "data/data_quality_report.json", "public/index.html"] + [
        "data/cleaned/%s.csv" % n for n in ("fund_nav", "debt", "gold", "nifty_tri", "nifty_valuation")]
    assert all(n in fmap for n in need), [n for n in need if n not in fmap]
    assert os.path.exists(os.path.join(out, "static", "index.html"))
    bundle = os.path.join(tmp, "bundle")
    for dst, src in fmap.items():
        d = os.path.join(bundle, dst); os.makedirs(os.path.dirname(d), exist_ok=True)
        shutil.copy(src if os.path.isabs(src) else os.path.join(proj, src), d)
    # Emulate Vercel routing from the built config.json: static output first (filesystem), then the function.
    # (This is my emulation of the documented routing order, NOT Vercel's CDN.)
    router = r'''
import sys, os, mimetypes, logging; logging.disable(logging.CRITICAL)
sys.path.insert(0, os.getcwd()); import app as A
STATIC = sys.argv[1]
def application(environ, start):
    p = environ["PATH_INFO"]; f = os.path.join(STATIC, "index.html" if p == "/" else p.lstrip("/"))
    if os.path.isfile(f) and os.path.commonpath([STATIC, os.path.realpath(f)]) == STATIC:
        body = open(f, "rb").read(); start("200 OK", [("Content-Type", mimetypes.guess_type(f)[0] or "text/html"), ("X-Served-By", "static")])
        return [body]
    return A.app(environ, start)
from werkzeug.serving import run_simple
run_simple("127.0.0.1", int(sys.argv[2]), application, threaded=True)
'''
    import socket, time, urllib.request
    sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]; sock.close()
    env = {"PATH": os.environ["PATH"], "PYTHONPATH": os.path.join(bundle, "_vendor")}
    srv = subprocess.Popen([sys.executable, "-c", router, os.path.realpath(os.path.join(out, "static")), str(port)],
                           cwd=bundle, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = "http://127.0.0.1:%d" % port
    def get(path, timeout=120):
        for _ in range(60):
            try:
                with urllib.request.urlopen(base + path, timeout=timeout) as r:
                    return r.status, r.headers.get("X-Served-By", "function"), r.read()
            except urllib.error.HTTPError as e:
                return e.code, "function", e.read()
            except OSError:
                time.sleep(0.5)
        raise SystemExit("server did not start")
    failures = []
    try:
        st, by, body = get("/")
        print("/", st, by, len(body), "bytes"); 
        if st != 200 or b"<html" not in body.lower(): failures.append("/ does not serve the frontend")
        h = json.loads(get("/api/health")[2])["data"]
        print("health ready=%s n_funds=%s source=%s" % (h["ready"], h["n_funds"], h["source"]))
        if not (h["ready"] and h["n_funds"] == 12): failures.append("health")
        d = json.loads(get("/api/datasets")[2]); print("datasets", json.dumps(d)[:200])
        n = len(json.loads(get("/api/funds")[2])["data"]["funds"] if "funds" in json.loads(get("/api/funds")[2])["data"] else json.loads(get("/api/funds")[2])["data"])
        print("funds", n)
        if n != 12: failures.append("funds != 12")
        for u in ("/api/optimize", "/api/montecarlo", "/api/hypotheses", "/api/scenario", "/api/frontier", "/api/meta",
                  "/api/allocation-history", "/api/data-quality", "/api/data-report", "/api/diagnostics", "/api/fund-metrics"):
            st = get(u)[0]; print(u, st)
            if st != 200: failures.append(u)
        print("diag:", json.loads(get("/api/health?light=1&diag=1")[2])["data"]["diagnostics"]["dataset_files"])
        if os.environ.get("E2E_BROWSER"):
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                b = pw.chromium.launch(); pg = b.new_page(); errs = []
                pg.on("pageerror", lambda e: errs.append(str(e))); bad = []
                pg.on("response", lambda r: bad.append((r.url, r.status)) if "/api/" in r.url and r.status >= 400 else None)
                if os.environ.get("E2E_CHARTJS"):   # sandbox has no internet: serve the CDN Chart.js from a local copy
                    pg.route("**/cdnjs.cloudflare.com/**", lambda rt: rt.fulfill(path=os.environ["E2E_CHARTJS"], content_type="application/javascript"))
                pg.goto(base + "/"); pg.wait_for_timeout(12000)
                txt = pg.inner_text("body"); print("browser: title=%r api_errors=%s js_errors=%s" % (pg.title(), bad, errs))
                print("browser body mentions funds:", "Parag Parikh" in txt, "len", len(txt)); b.close()
                if bad or errs or "Parag Parikh" not in txt: failures.append("browser")
    finally:
        srv.terminate()
    print("BUILD, BUNDLE AND ROUTING EMULATION OK" if not failures else "FAILED: %s" % failures)
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
