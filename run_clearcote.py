"""Focused runner: benchmark ONLY the clearcote engine against all targets.
Avoids importing config.engines (which would require every browser package installed).

Usage:
  python run_clearcote.py                 # both variants, all bypass targets + creepjs
  python run_clearcote.py --headful       # only headful
  python run_clearcote.py --only kasada_protected,cloudflare_protected
"""
import asyncio, json, os, sys, time
from datetime import datetime
from urllib.parse import urlparse

# CLEARCOTE_LICENSE_KEY must be set in the environment (PRO v150 build). No secrets in source.
from config.benchmark_targets import benchmark_targets_config
from config.settings import settings
from engines.playwright.clearcote_engine import ClearcoteEngine

# Proxy comes from the BENCH_PROXY env var (a full URL: http://user:pass@host:port) or is
# omitted for a direct connection. Never hardcode credentials here.
PROXY = None
_purl = os.environ.get("BENCH_PROXY")
if _purl and "--noproxy" not in sys.argv:
    u = urlparse(_purl)
    PROXY = {"protocol": u.scheme, "host": u.hostname, "port": str(u.port or 8080)}
    if u.username and u.password:
        PROXY["username"] = u.username
        PROXY["password"] = u.password

# google_search is not a bot-detection vendor and is proxy-bypassed -> unreliable; skip by default
SKIP = {"google_search"}

ONLY = None
for a in sys.argv:
    if a.startswith("--only"):
        ONLY = set(a.split("=", 1)[1].split(",")) if "=" in a else None
VARIANTS = []
if "--headful" in sys.argv: VARIANTS = [("clearcote", False)]
elif "--headless" in sys.argv: VARIANTS = [("clearcote_headless", True)]
else: VARIANTS = [("clearcote", False), ("clearcote_headless", True)]


async def run_variant(name, headless):
    bt = benchmark_targets_config.bypass_targets
    dt = benchmark_targets_config.browser_data_targets
    targets = [t.model_dump() for t in bt.targets]
    dtargets = [t.model_dump() for t in dt.targets]
    if ONLY:
        targets = [t for t in targets if t["name"] in ONLY]
        dtargets = [t for t in dtargets if t["name"] in ONLY]
    else:
        targets = [t for t in targets if t["name"] not in SKIP]

    print(f"\n{'='*70}\n### VARIANT: {name} (headless={headless})  proxy=DataImpulse\n{'='*70}", flush=True)
    engine = ClearcoteEngine(name=name, headless=headless, proxy=PROXY)
    rows = []
    data = {}
    try:
        await engine.start()
        print(f"[started {name}]", flush=True)
        for t in targets:
            r = {"target": t["name"], "url": t["url"], "bypass": False, "err": None, "ms": 0}
            try:
                t0 = time.time()
                await engine.navigate(t["url"])
                await asyncio.sleep(settings.browser.page_stabilization_delay_s)
                r["ms"] = int((time.time()-t0)*1000)
                fn = bt.checkers.get(t["check_function"])
                r["bypass"] = bool(await fn(engine))
            except Exception as e:
                r["err"] = f"{type(e).__name__}: {str(e)[:70]}"
            rows.append(r)
            print(f"  [{'PASS' if r['bypass'] else 'BLOCK'}] {r['target']:24} {r['ms']}ms {r['err'] or ''}", flush=True)
        data = {}
        for t in dtargets:
            try:
                await engine.navigate(t["url"]); await asyncio.sleep(settings.browser.page_stabilization_delay_s)
                fn = dt.checkers.get(t["check_function"])
                data[t["name"]] = await fn(engine)
            except Exception as e:
                data[t["name"]] = {"err": f"{type(e).__name__}: {str(e)[:60]}"}
            print(f"  [DATA] {t['name']}: {json.dumps(data[t['name']])[:120]}", flush=True)
    except Exception as e:
        print(f"[CRITICAL {name}]: {type(e).__name__}: {e}", flush=True)
    finally:
        try: await engine.stop()
        except Exception: pass
    passed = sum(1 for r in rows if r["bypass"]); total = len(rows)
    print(f"\n### {name} BYPASS RATE: {passed}/{total} = {100*passed/max(total,1):.0f}%", flush=True)
    return {"variant": name, "headless": headless, "rows": rows, "data": data,
            "bypass_rate": passed/max(total,1)}


async def main():
    out = []
    for name, hl in VARIANTS:
        out.append(await run_variant(name, hl))
    os.makedirs("results", exist_ok=True)
    fn = f"results/clearcote_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    json.dump(out, open(fn, "w"), indent=2)
    print(f"\nSaved {fn}")
    print("\n===== FINAL SUMMARY =====")
    for o in out:
        print(f"{o['variant']:20} bypass_rate={o['bypass_rate']*100:.0f}%")
        for r in o["rows"]:
            print(f"    {'PASS ' if r['bypass'] else 'BLOCK'} {r['target']:24} {r['err'] or ''}")

if __name__ == "__main__":
    asyncio.run(main())
