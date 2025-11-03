#!/usr/bin/env python3
# Sparrow Hub - multi-agent aggregator/proxy for Sparrow-WiFi Agent

import asyncio, os, sys, re
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
import yaml
import uvicorn
import httpx
from fastapi import FastAPI, HTTPException, Query, Response

CFG_PATH = os.environ.get("SPARROW_HUB_CFG", "hub_policy.yml")

@dataclass
class Config:
    agents: Dict[str, str]
    mode_networks: str                 # "single" | "agg_counts" | "full"
    preferred_agent: Optional[str]
    spectrum_24: Optional[str]
    spectrum_5: Optional[str]
    falcon_targets: List[str]
    bt_targets: List[str]
    record_targets: List[str]
    top_ssids_per_channel: int
    min_rssi_dbm: int
    allow_multi_deauth: bool
    control_propagate_deauth: bool
    control_propagate_bt: bool
    control_propagate_record: bool

    @staticmethod
    def from_yaml(d: dict) -> "Config":
        mode = d.get("mode", {})
        spectrum = d.get("spectrum", {})
        defaults = d.get("defaults", {})
        limits = d.get("limits", {})
        safety = d.get("safety", {})
        control = d.get("control", {})
        agents = d.get("agents", {})
        preferred = mode.get("preferred_agent")
        if preferred is None and agents:
            preferred = next(iter(agents.keys()))
        return Config(
            agents=agents,
            mode_networks=mode.get("networks", "single"),
            preferred_agent=preferred,
            spectrum_24=spectrum.get("source_agent_24"),
            spectrum_5=spectrum.get("source_agent_5"),
            falcon_targets=defaults.get("falcon_targets", [preferred] if preferred else []),
            bt_targets=defaults.get("bt_targets", [preferred] if preferred else []),
            record_targets=defaults.get("record_targets", [preferred] if preferred else []),
            top_ssids_per_channel=int(limits.get("top_ssids_per_channel", 50)),
            min_rssi_dbm=int(limits.get("min_rssi_dbm", -92)),
            allow_multi_deauth=bool(safety.get("allow_multi_deauth", False)),
            control_propagate_deauth=bool(control.get("propagate_deauth", True)),
            control_propagate_bt=bool(control.get("propagate_bt", True)),
            control_propagate_record=bool(control.get("propagate_record", False)),
        )

    def to_yaml(self) -> dict:
        return {
            "agents": self.agents,
            "mode": {"networks": self.mode_networks, "preferred_agent": self.preferred_agent},
            "spectrum": {"source_agent_24": self.spectrum_24, "source_agent_5": self.spectrum_5},
            "defaults": {
                "falcon_targets": self.falcon_targets,
                "bt_targets": self.bt_targets,
                "record_targets": self.record_targets,
            },
            "limits": {"top_ssids_per_channel": self.top_ssids_per_channel, "min_rssi_dbm": self.min_rssi_dbm},
            "safety": {"allow_multi_deauth": self.allow_multi_deauth},
            "control": {
                "propagate_deauth": self.control_propagate_deauth,
                "propagate_bt": self.control_propagate_bt,
                "propagate_record": self.control_propagate_record,
            },
        }

conf: Config = None
client = httpx.AsyncClient(timeout=20.0)
app = FastAPI(title="Sparrow Hub")

# Selection trackers
_current_agent: Optional[str] = None
_current_iface: Optional[str] = None
_last_falcon_agent: Optional[str] = None   # <— key fix: drives getscanresults routing

# ---------------------- utils ----------------------

def load_config() -> Config:
    if not os.path.exists(CFG_PATH):
        raise RuntimeError(f"Missing config: {CFG_PATH}")
    with open(CFG_PATH, "r") as f:
        return Config.from_yaml(yaml.safe_load(f) or {})

def save_config():
    with open(CFG_PATH, "w") as f:
        yaml.safe_dump(conf.to_yaml(), f, sort_keys=False)

def split_iface(ns_iface: str) -> Tuple[Optional[str], str]:
    if "/" in ns_iface:
        a, b = ns_iface.split("/", 1)
        return a, b
    return None, ns_iface

def pick_agent(explicit: Optional[str] = None, allow_none: bool = False) -> Optional[str]:
    agent = explicit or _current_agent or conf.preferred_agent
    if agent and agent in conf.agents:
        return agent
    if not agent and len(conf.agents) == 1:
        return next(iter(conf.agents.keys()))
    if allow_none:
        return None
    raise HTTPException(400, "No agent selected and no preferred_agent configured")

async def agent_get_json(agent_id: str, path: str):
    base = conf.agents.get(agent_id)
    if not base:
        raise HTTPException(404, f"Unknown agent '{agent_id}'")
    url = base.rstrip("/") + path
    r = await client.get(url)
    if r.status_code != 200:
        detail = r.text[:400] if r.text else f"status {r.status_code}"
        raise HTTPException(r.status_code, f"{agent_id} GET {path} failed: {detail}")
    try:
        return r.json()
    except Exception:
        return {"raw": r.text}

async def agent_get_raw(agent_id: str, path: str) -> Response:
    """Return byte-for-byte JSON from agent (avoids schema quirks)."""
    base = conf.agents.get(agent_id)
    if not base:
        raise HTTPException(404, f"Unknown agent '{agent_id}'")
    url = base.rstrip("/") + path
    r = await client.get(url)
    if r.status_code != 200:
        detail = r.text[:400] if r.text else f"status {r.status_code}"
        raise HTTPException(r.status_code, f"{agent_id} GET {path} failed: {detail}")
    # Pass-through
    return Response(content=r.content, media_type=r.headers.get("content-type", "application/json"))

async def agent_post_json(agent_id: str, path: str, payload: dict):
    base = conf.agents.get(agent_id)
    if not base:
        raise HTTPException(404, f"Unknown agent '{agent_id}'")
    url = base.rstrip("/") + path
    r = await client.post(url, json=payload)
    if r.status_code != 200:
        detail = r.text[:400] if r.text else f"status {r.status_code}"
        raise HTTPException(r.status_code, f"{agent_id} POST {path} failed: {detail}")
    try:
        return r.json()
    except Exception:
        return {"raw": r.text}

async def wait_for_mon_change(agent_id: str, want_iface: str, present: bool, timeout_s: float = 2.5):
    end = asyncio.get_event_loop().time() + timeout_s
    while True:
        try:
            res = await agent_get_json(agent_id, "/wireless/moninterfaces")
            mon = set(res.get("interfaces", []) or [])
            if (want_iface in mon) == present:
                return True
        except Exception:
            pass
        if asyncio.get_event_loop().time() >= end:
            return False
        await asyncio.sleep(0.2)

def looks_like_already_stopped(msg: str) -> bool:
    return bool(re.search(r"(not\s+found|no\s+such|does\s+not\s+exist|already\s+stopp?ed|monitor\s+mode\s+not\s+enabled)", msg, re.I))

def ok():  return {"errcode": 0, "errmsg": ""}
def err(m): return {"errcode": 1, "errmsg": str(m)}

# ---------------------- lifecycle ----------------------

@app.on_event("startup")
async def _startup():
    global conf
    conf = load_config()

@app.on_event("shutdown")
async def _shutdown():
    await client.aclose()

# ---------------------- admin ----------------------

@app.get("/agents")
async def list_agents():
    return {"agents": conf.agents}

@app.post("/admin/reload")
async def admin_reload():
    global conf
    conf = load_config()
    return {"status": "reloaded", "agents": conf.agents}

@app.post("/admin/save")
async def admin_save():
    save_config(); return {"status": "saved"}

# ---------------------- wireless ----------------------

@app.get("/wireless/interfaces")
async def wireless_interfaces():
    tasks = [agent_get_json(aid, "/wireless/interfaces") for aid in conf.agents.keys()]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    namespaced: List[str] = []
    for (aid, res) in zip(conf.agents.keys(), results):
        if isinstance(res, Exception):
            continue
        for iface in res.get("interfaces", []) or []:
            namespaced.append(f"{aid}/{iface}")
    return {"interfaces": namespaced}

@app.get("/wireless/networks/{iface:path}")
async def wireless_networks(iface: str):
    global _current_agent, _current_iface
    agent_id, base_iface = split_iface(iface)
    if agent_id:
        _current_agent, _current_iface = agent_id, base_iface
        return await agent_get_json(agent_id, f"/wireless/networks/{base_iface}")

    # aggregate by plain iface across agents (best RSSI wins)
    tasks = [agent_get_json(aid, f"/wireless/networks/{base_iface}") for aid in conf.agents.keys()]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    merged = {}
    for (aid, res) in zip(conf.agents.keys(), results):
        if isinstance(res, Exception):
            continue
        for net in res.get("networks", []) or []:
            try:
                bssid = net.get("macAddr","").lower()
                ssid  = net.get("ssid","")
                ch    = int(net.get("channel",0))
                key = (bssid, ssid, ch)
                rssi = int(net.get("signal",-1000))
            except Exception:
                continue
            if rssi < conf.min_rssi_dbm:
                continue
            cur = merged.get(key)
            if not cur or rssi > int(cur.get("signal",-1000)):
                net2 = dict(net); net2["source_agent_id"] = aid
                merged[key] = net2
    nets = list(merged.values())
    return {"errCode": 0, "errString": "", "networks": nets}

@app.get("/wireless/moninterfaces")
async def wireless_moninterfaces(agent: Optional[str] = Query(default=None)):
    agent_id = pick_agent(agent)
    return await agent_get_json(agent_id, "/wireless/moninterfaces")

# ---------------------- falcon ----------------------

@app.get("/falcon/scanrunning/")
async def falcon_scanrunning(agent: Optional[str] = Query(default=None)):
    agent_id = pick_agent(agent)
    return await agent_get_json(agent_id, "/falcon/scanrunning/")

@app.get("/falcon/scanrunning/{iface:path}")
async def falcon_scanrunning_iface(iface: str, agent: Optional[str] = Query(default=None)):
    global _current_agent, _current_iface
    agent_id_ns, base_iface = split_iface(iface)
    agent_id = agent_id_ns or pick_agent(agent)
    _current_agent, _current_iface = agent_id, base_iface
    try:
        return await agent_get_json(agent_id, f"/falcon/scanrunning/{base_iface}")
    except HTTPException as e:
        if e.status_code != 404:
            raise
        return await agent_get_json(agent_id, "/falcon/scanrunning/")

@app.get("/falcon/startmonmode/{iface:path}")
async def falcon_startmonmode(iface: str):
    global _current_agent, _current_iface
    agent_id_ns, base_iface = split_iface(iface)
    target = agent_id_ns or pick_agent()
    _current_agent, _current_iface = target, base_iface
    res = await agent_get_json(target, f"/falcon/startmonmode/{base_iface}")
    want = f"{base_iface}mon" if not base_iface.endswith("mon") else base_iface
    await wait_for_mon_change(target, want, True)
    await asyncio.sleep(0.25)
    return res if isinstance(res, dict) else ok()

@app.get("/falcon/stopmonmode/{iface:path}")
async def falcon_stopmonmode(iface: str):
    agent_id_ns, base_iface = split_iface(iface)
    target = agent_id_ns or pick_agent()
    try:
        res = await agent_get_json(target, f"/falcon/stopmonmode/{base_iface}")
        if isinstance(res, dict) and res.get("errcode", 0) != 0:
            if looks_like_already_stopped((res.get("errmsg") or "")):
                res = ok()
        await wait_for_mon_change(target, base_iface, False)
        await asyncio.sleep(0.25)
        return res if isinstance(res, dict) else ok()
    except HTTPException as e:
        if looks_like_already_stopped(str(e.detail)):
            return ok()
        raise

@app.get("/falcon/startscan/{iface:path}")
async def falcon_startscan(iface: str):
    global _current_agent, _current_iface, _last_falcon_agent
    agent_id_ns, base_iface = split_iface(iface)
    target = agent_id_ns or pick_agent()
    _current_agent, _current_iface = target, base_iface
    _last_falcon_agent = target                    # <— remember where results should come from
    res = await agent_get_json(target, f"/falcon/startscan/{base_iface}")
    return res if isinstance(res, dict) else ok()

# Raw pass-through of results (no reshaping)
@app.get("/falcon/getscanresults")
async def falcon_getscanresults():
    # Always route to the last agent that started a scan; fall back to preferred/only.
    target = _last_falcon_agent or pick_agent(allow_none=True)
    if not target:
        return Response(content=b'{"aps":[],"clients":[]}', media_type="application/json")
    return await agent_get_raw(target, "/falcon/getscanresults")

@app.get("/falcon/getscanresults/{iface:path}")
async def falcon_getscanresults_iface(iface: str):
    agent_ns, _ = split_iface(iface)
    target = agent_ns or _last_falcon_agent or pick_agent(allow_none=True)
    if not target:
        return Response(content=b'{"aps":[],"clients":[]}', media_type="application/json")
    return await agent_get_raw(target, "/falcon/getscanresults")

# Stop scan (iface-aware and generic); never throw GUI-popups for benign states
@app.get("/falcon/stopscan/{iface:path}")
async def falcon_stopscan_iface(iface: str):
    agent_ns, _ = split_iface(iface)
    target = agent_ns or _last_falcon_agent or pick_agent(allow_none=True)
    if not target:
        return ok()
    try:
        res = await agent_get_json(target, "/falcon/stopscan")
        return res if isinstance(res, dict) else ok()
    except HTTPException as e:
        if e.status_code == 400 or "Bad Request" in str(e.detail):
            return ok()
        return err(str(e.detail))

@app.get("/falcon/stopscan")
async def falcon_stopscan_generic():
    target = _last_falcon_agent or pick_agent(allow_none=True)
    if not target:
        return ok()
    try:
        res = await agent_get_json(target, "/falcon/stopscan")
        return res if isinstance(res, dict) else ok()
    except HTTPException as e:
        if e.status_code == 400 or "Bad Request" in str(e.detail):
            return ok()
        return err(str(e.detail))

# ---------------------- gps/system (unchanged) ----------------------

@app.get("/gps/status")
async def gps_status(agent: Optional[str] = Query(default=None)):
    agent_id = pick_agent(agent)
    return await agent_get_json(agent_id, "/gps/status")


# ---------------------- bluetooth + spectrum passthroughs ----------------------

@app.get("/bluetooth/running")
async def bt_running(agent: Optional[str] = Query(default=None)):
    agent_id = pick_agent(agent)
    return await agent_get_json(agent_id, "/bluetooth/running")

@app.get("/bluetooth/scanstatus")
async def bt_scanstatus(agent: Optional[str] = Query(default=None)):
    agent_id = pick_agent(agent)
    return await agent_get_raw(agent_id, "/bluetooth/scanstatus")

@app.get("/spectrum/hackrfstatus")
async def spectrum_hackrfstatus(agent: Optional[str] = Query(default=None)):
    agent_id = pick_agent(agent)
    return await agent_get_json(agent_id, "/spectrum/hackrfstatus")

@app.get("/spectrum/scanstatus")
async def spectrum_scanstatus(agent: Optional[str] = Query(default=None)):
    agent_id = pick_agent(agent)
    return await agent_get_raw(agent_id, "/spectrum/scanstatus")

# ---------------------- bluetooth control passthroughs (safe, GET-forwarding) ----------------------

@app.get("/bluetooth/present")
async def bt_present(agent: Optional[str] = Query(default=None)):
    agent_id = pick_agent(agent)
    return await agent_get_json(agent_id, "/bluetooth/present")

@app.get("/bluetooth/scanstart")
async def bt_scanstart(agent: Optional[str] = Query(default=None)):
    agent_id = pick_agent(agent)
    return await agent_get_json(agent_id, "/bluetooth/scanstart")

@app.get("/bluetooth/scanstop")
async def bt_scanstop(agent: Optional[str] = Query(default=None)):
    agent_id = pick_agent(agent)
    return await agent_get_json(agent_id, "/bluetooth/scanstop")

@app.get("/bluetooth/beaconstart")
async def bt_beaconstart(agent: Optional[str] = Query(default=None)):
    agent_id = pick_agent(agent)
    return await agent_get_json(agent_id, "/bluetooth/beaconstart")

@app.get("/bluetooth/beaconstop")
async def bt_beaconstop(agent: Optional[str] = Query(default=None)):
    agent_id = pick_agent(agent)
    return await agent_get_json(agent_id, "/bluetooth/beaconstop")

# Discovery (Promiscuous vs LE Advertisement)
@app.get("/bluetooth/discoverystartp")
async def bt_discoverystart_promisc(agent: Optional[str] = Query(default=None)):
    agent_id = pick_agent(agent)
    return await agent_get_json(agent_id, "/bluetooth/discoverystartp")

@app.get("/bluetooth/discoverystarta")
async def bt_discoverystart_adv(agent: Optional[str] = Query(default=None)):
    agent_id = pick_agent(agent)
    return await agent_get_json(agent_id, "/bluetooth/discoverystarta")

@app.get("/bluetooth/discoverystop")
async def bt_discoverystop(agent: Optional[str] = Query(default=None)):
    agent_id = pick_agent(agent)
    return await agent_get_json(agent_id, "/bluetooth/discoverystop")

@app.get("/bluetooth/discoveryclear")
async def bt_discoveryclear(agent: Optional[str] = Query(default=None)):
    agent_id = pick_agent(agent)
    return await agent_get_json(agent_id, "/bluetooth/discoveryclear")

@app.get("/bluetooth/discoverystatus")
async def bt_discoverystatus(agent: Optional[str] = Query(default=None)):
    agent_id = pick_agent(agent)
    return await agent_get_raw(agent_id, "/bluetooth/discoverystatus")

# ---------------------- tiny CLI (optional) ----------------------

async def cli():
    loop = asyncio.get_event_loop()
    print("Sparrow Hub CLI ready. Type 'help'. (Ctrl-C to exit)")
    while True:
        cmd = await loop.run_in_executor(None, sys.stdin.readline)
        if not cmd:
            break
        parts = cmd.strip().split()
        if not parts:
            continue
        try:
            if parts[0]=="help":
                print("Commands:\n  list\n  save")
            elif parts[0]=="list":
                print(conf.agents)
            elif parts[0]=="save":
                save_config(); print("saved")
            else:
                print("Unknown.")
        except Exception as e:
            print("Error:", e)

def run():
    loop = asyncio.get_event_loop()
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=8020, log_level="info"))
    async def main():
        app_task = asyncio.create_task(server.serve())
        cli_task = asyncio.create_task(cli())
        await asyncio.wait([app_task, cli_task], return_when=asyncio.FIRST_COMPLETED)
    loop.run_until_complete(main())

if __name__ == "__main__":
    run()


# ===================== health-aware agent selection helpers =====================
from typing import Optional, Tuple

# Expect a global 'conf' and helpers: pick_agent(agent: Optional[str]) -> str,
# agent_get_json(agent_id: str, path: str), agent_get_raw(agent_id: str, path: str)
# We'll add wrappers that try preferred, then fail over to any alive agent.

async def agent_alive(agent_id: str) -> bool:
    try:
        # Cheap/fast probe that's present on every agent
        await agent_get_json(agent_id, "/wireless/interfaces")
        return True
    except Exception:
        return False

async def pick_agent_live(agent: Optional[str] = None) -> Optional[str]:
    """Return a live agent id, preferring explicit 'agent', then conf.mode.preferred_agent, then any alive."""
    # 1) explicit
    if agent:
        return agent if await agent_alive(agent) else None
    # 2) preferred_agent from config
    try:
        preferred = getattr(conf, "mode", {}).get("preferred_agent")  # conf.mode.preferred_agent
    except Exception:
        preferred = None
    if preferred and await agent_alive(preferred):
        return preferred
    # 3) any live agent
    for aid in getattr(conf, "agents", {}).keys():
        if await agent_alive(aid):
            return aid
    return None

async def pick_spectrum_agent(band: str, agent: Optional[str] = None) -> Optional[str]:
    """band: '24' or '5'"""
    # If explicit, honor it
    if agent:
        return agent if await agent_alive(agent) else None
    # Try band-specific source_agent if configured
    try:
        src = getattr(conf, "spectrum", {}).get("source_agent_24" if band == "24" else "source_agent_5")
    except Exception:
        src = None
    if src and await agent_alive(src):
        return src
    # Fallback to preferred/any
    return await pick_agent_live()

# ===================== spectrum control passthroughs =====================

@app.get("/spectrum/scanstart24")
async def spectrum_scanstart24(agent: Optional[str] = Query(default=None)):
    target = await pick_spectrum_agent("24", agent)
    if not target:
        return JSONResponse({"errcode": 1, "errmsg": "No live agent available"}, status_code=503)
    return await agent_get_json(target, "/spectrum/scan/start24")

@app.get("/spectrum/scanstart5")
async def spectrum_scanstart5(agent: Optional[str] = Query(default=None)):
    target = await pick_spectrum_agent("5", agent)
    if not target:
        return JSONResponse({"errcode": 1, "errmsg": "No live agent available"}, status_code=503)
    return await agent_get_json(target, "/spectrum/scan/start5")

@app.get("/spectrum/scanstop")
async def spectrum_scanstop(agent: Optional[str] = Query(default=None)):
    # Stop whichever agent is designated for either band, but prefer explicit
    target = await pick_agent_live(agent)
    if not target:
        return JSONResponse({"errcode": 1, "errmsg": "No live agent available"}, status_code=503)
    return await agent_get_json(target, "/spectrum/scan/stop")

# Keep existing status endpoints; we already had /spectrum/hackrfstatus and /spectrum/scanstatus

# ===================== admin quality-of-life (optional) =====================
# You can hit these from a CLI/script. They don't change any existing behavior.
#  - /admin/refresh  : reload policy file at runtime (if your hub implements save/load)
#  - /admin/set_preferred?agent=west : set preferred at runtime (in-memory; call save to persist)

try:
    @app.get("/admin/refresh")
    async def admin_refresh():
        try:
            load_config()
            return {"ok": True, "msg": "config reloaded"}
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    @app.get("/admin/set_preferred")
    async def admin_set_preferred(agent: Optional[str] = Query(default=None)):
        if not agent:
            return JSONResponse({"ok": False, "error": "missing ?agent="}, status_code=400)
        if agent not in getattr(conf, "agents", {}):
            return JSONResponse({"ok": False, "error": f"unknown agent '{agent}'"}, status_code=404)
        if not await agent_alive(agent):
            return JSONResponse({"ok": False, "error": f"agent '{agent}' appears down"}, status_code=503)
        # set in-memory; you can /save afterward if your hub already supports save_config()
        try:
            conf.mode["preferred_agent"] = agent
        except Exception:
            # fallback if conf.mode isn't a dict-like
            setattr(conf, "mode", {"preferred_agent": agent})
        return {"ok": True, "preferred_agent": agent}
except Exception:
    # Hub may not define load_config/save_config; ignore if not present
    pass
