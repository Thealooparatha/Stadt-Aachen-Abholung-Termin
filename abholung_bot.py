#!/usr/bin/env python3
"""
Aachen Ausländeramt - Abholung Aufenthaltstitel appointment checker.

Polls the StädteRegion Aachen booking system
(termine.staedteregion-aachen.de/auslaenderamt) for free "Abholung" slots
(eAT pickup at Aachen Arkaden, Trierer Straße 1) and notifies you when
new dates appear. It only CHECKS; you book yourself via the link it sends.

Notifications (pick one or more):
  --ntfy-topic TOPIC          push to your phone via the free ntfy app (easiest)
  --telegram-token / --telegram-chat-id   push via your own Telegram bot
  --whatsapp-phone / --whatsapp-apikey    WhatsApp message via CallMeBot (free)
  (always) console output + terminal bell

Examples:
  python abholung_bot.py --once                      # single check, print result
  python abholung_bot.py --ntfy-topic habib-eat-1234  # loop every ~5 min
  python abholung_bot.py --ntfy-topic habib-eat-1234 --max-days 30 --interval 180
"""

import argparse
import datetime as dt
import json
import os
import random
import re
import sys
import time

import requests
from bs4 import BeautifulSoup

BASE = "https://termine.staedteregion-aachen.de/auslaenderamt"
START_URL = f"{BASE}/select2?md=1"
SUGGEST_URL = f"{BASE}/suggest"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
NO_SLOT_TEXT = "Kein freier Termin verfügbar"
DATE_RE = re.compile(r"(\d{2}\.\d{2}\.\d{4})")


# --------------------------------------------------------------------------
# Scraping
# --------------------------------------------------------------------------
class SiteChanged(Exception):
    """The page structure no longer matches what the script expects."""


def find_concern_id(soup, keyword, option_index):
    """Find the 'cnc' id of the service under the <h3> whose text contains keyword."""
    h3 = soup.find("h3", string=lambda s: bool(s) and keyword.lower() in s.lower())
    if not h3:
        headings = [h.get_text(strip=True) for h in soup.find_all("h3")]
        raise SiteChanged(
            f"No section heading containing '{keyword}'. Headings found: {headings}"
        )
    container = h3.find_next_sibling()
    items = container.find_all("li") if container else []
    if not items:
        raise SiteChanged(f"No services listed under '{h3.get_text(strip=True)}'.")
    if option_index >= len(items):
        raise SiteChanged(
            f"Option index {option_index} out of range ({len(items)} options)."
        )
    li = items[option_index]
    cnc_id = (li.get("id") or "").split("-")[-1]
    if not cnc_id.isdigit():
        raise SiteChanged(f"Could not read service id from <li id='{li.get('id')}'>.")
    label = " ".join(li.get_text(" ", strip=True).split())
    return cnc_id, label


def check_abholung(keyword="Abholung", option_index=0, timeout=30):
    """
    Returns (dates, service_label). dates is a sorted list of datetime.date.
    Empty list means no free slot right now.
    """
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "de-DE,de;q=0.9"})

    # 1) Service selection page -> find the Abholung service id
    r1 = s.get(START_URL, timeout=timeout)
    r1.raise_for_status()
    cnc_id, label = find_concern_id(
        BeautifulSoup(r1.text, "html.parser"), keyword, option_index
    )

    # 2) Location page for that service
    loc_url = f"{BASE}/location?mdt=89&select_cnc=1&cnc-{cnc_id}=1"
    r2 = s.get(loc_url, timeout=timeout)
    r2.raise_for_status()
    soup2 = BeautifulSoup(r2.text, "html.parser")
    loc_input = soup2.find("input", {"name": "loc"})
    if not loc_input:
        raise SiteChanged("Location page has no 'loc' field.")

    # Build the form payload from the page itself instead of hard-coding it
    payload = {"loc": loc_input.get("value", ""), "gps_lat": "50.7753", "gps_long": "6.0839"}
    form = loc_input.find_parent("form")
    submit = None
    if form:
        submit = form.find(["input", "button"], {"name": "select_location"})
    payload["select_location"] = (
        submit.get("value") if submit and submit.get("value") else "Standort auswählen"
    )

    # 3) Choose the location, then 4) read suggested slots
    s.post(loc_url, data=payload, timeout=timeout).raise_for_status()
    r4 = s.get(SUGGEST_URL, timeout=timeout)
    r4.raise_for_status()
    html = r4.text

    if NO_SLOT_TEXT in html:
        return [], label

    soup4 = BeautifulSoup(html, "html.parser")
    texts = []
    acc = soup4.find("div", {"id": "sugg_accordion"})
    if acc:
        texts += [h.get_text(" ", strip=True) for h in acc.find_all("h3")]
    summary = soup4.find("summary", id="suggest_details_summary")
    if summary:
        texts.append(summary.get_text(" ", strip=True))

    dates = set()
    for t in texts:
        for d in DATE_RE.findall(t):
            try:
                dates.add(dt.datetime.strptime(d, "%d.%m.%Y").date())
            except ValueError:
                pass

    if not dates:
        # The "no slot" text is missing but no dates were parsed either.
        raise SiteChanged(
            "Suggest page has neither 'no slot' text nor readable dates "
            "(could mean slots exist in a new layout, check manually)."
        )
    return sorted(dates), label


# --------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------
def notify(args, title, message):
    print(f"\a[{now()}] {title}\n{message}\n", flush=True)

    if args.ntfy_topic:
        try:
            requests.post(
                f"https://ntfy.sh/{args.ntfy_topic}",
                data=message.encode("utf-8"),
                headers={
                    "Title": title.encode("utf-8").decode("latin-1", "ignore"),
                    "Priority": "high",
                    "Tags": "calendar",
                    "Click": START_URL,
                },
                timeout=15,
            )
        except requests.RequestException as e:
            print(f"[{now()}] ntfy failed: {e}", file=sys.stderr)

    if args.telegram_token and args.telegram_chat_id:
        try:
            requests.post(
                f"https://api.telegram.org/bot{args.telegram_token}/sendMessage",
                json={
                    "chat_id": args.telegram_chat_id,
                    "text": f"{title}\n{message}",
                    "disable_web_page_preview": True,
                },
                timeout=15,
            )
        except requests.RequestException as e:
            print(f"[{now()}] Telegram failed: {e}", file=sys.stderr)

    if args.whatsapp_phone and args.whatsapp_apikey:
        try:
            r = requests.get(
                "https://api.callmebot.com/whatsapp.php",
                params={
                    "phone": args.whatsapp_phone,
                    "apikey": args.whatsapp_apikey,
                    "text": f"{title}\n{message}",
                },
                timeout=30,
            )
            if r.status_code != 200:
                print(f"[{now()}] WhatsApp failed: HTTP {r.status_code} {r.text[:200]}",
                      file=sys.stderr)
        except requests.RequestException as e:
            print(f"[{now()}] WhatsApp failed: {e}", file=sys.stderr)


def now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def fmt(dates):
    return "\n".join(d.strftime("%a %d.%m.%Y") for d in dates)


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(description="Aachen eAT Abholung appointment checker")
    p.add_argument("--interval", type=int, default=int(os.getenv("INTERVAL") or 300),
                   help="seconds between checks (default 300, minimum 60)")
    p.add_argument("--max-days", type=int, default=0,
                   help="only alert for slots within N days from today (0 = any)")
    p.add_argument("--until", default=os.getenv("UNTIL"),
                   help="only alert for slots on or before this date, YYYY-MM-DD")
    p.add_argument("--repeat", action="store_true",
                   default=os.getenv("REPEAT", "").lower() in ("1", "true", "yes"),
                   help="message on every check while slots exist, not only for new dates")
    p.add_argument("--keyword", default="Abholung",
                   help="text of the service section heading to look for")
    p.add_argument("--option-index", type=int, default=0,
                   help="which service under that heading (0 = first)")
    p.add_argument("--once", action="store_true", help="check once and exit")
    p.add_argument("--test-notify", action="store_true",
                   help="send a test notification and exit")
    p.add_argument("--ntfy-topic", default=os.getenv("NTFY_TOPIC"))
    p.add_argument("--telegram-token", default=os.getenv("TELEGRAM_TOKEN"))
    p.add_argument("--telegram-chat-id", default=os.getenv("TELEGRAM_CHAT_ID"))
    p.add_argument("--whatsapp-phone", default=os.getenv("WHATSAPP_PHONE"),
                   help="your WhatsApp number incl. country code, e.g. +4915112345678")
    p.add_argument("--whatsapp-apikey", default=os.getenv("WHATSAPP_APIKEY"),
                   help="CallMeBot API key")
    p.add_argument("--state-file", default=os.getenv("STATE_FILE"),
                   help="remember notified dates across restarts (JSON file)")
    p.add_argument("--max-runtime", type=int, default=0,
                   help="stop after N minutes (0 = run forever); used for cloud runs")
    args = p.parse_args()

    if args.test_notify:
        notify(args, "Test: Abholung bot", "Notifications work. Book here:\n" + START_URL)
        return

    until = dt.date.fromisoformat(args.until) if args.until else None
    interval = max(args.interval, 60)
    deadline = time.time() + args.max_runtime * 60 if args.max_runtime else None
    state = load_state(args.state_file)
    seen = state["seen"]   # dates already reported, so you are not spammed
    fail_streak = 0

    print(f"[{now()}] Watching Abholung appointments (every ~{interval}s). Ctrl+C to stop.")
    while True:
        try:
            dates, label = check_abholung(args.keyword, args.option_index)
            fail_streak = 0
            state["site_error_sent"] = False
            if state["net_error_sent"]:
                notify(args, "Abholung bot: back online", "The booking site is reachable again.")
                state["net_error_sent"] = False

            if args.max_days:
                limit = dt.date.today() + dt.timedelta(days=args.max_days)
                dates = [d for d in dates if d <= limit]
            if until:
                dates = [d for d in dates if d <= until]

            if dates:
                new = [d for d in dates if d not in seen]
                if new or args.repeat:
                    notify(
                        args,
                        "Abholung appointment available!",
                        f"{label}\n{'New dates' if new else 'Available dates'}:\n"
                        f"{fmt(new or dates)}\n\nBook now: {START_URL}",
                    )
                else:
                    print(f"[{now()}] Still available (already notified): {fmt(dates).replace(chr(10), ', ')}")
                seen = set(dates)  # forget dates that disappeared, so they alert again if they return
            else:
                print(f"[{now()}] No free slot{f' up to {until:%d.%m.%Y}' if until else ''} ({label}).")
                seen = set()

        except SiteChanged as e:
            print(f"[{now()}] Site layout problem: {e}", file=sys.stderr)
            if not state["site_error_sent"]:
                notify(args, "Abholung bot: check manually",
                       f"{e}\n\nOpen: {START_URL}")
                state["site_error_sent"] = True
        except requests.RequestException as e:
            fail_streak += 1
            print(f"[{now()}] Network error ({fail_streak}): {e}", file=sys.stderr)
            if fail_streak >= 3 and not state["net_error_sent"]:
                notify(args, "Abholung bot: cannot reach booking site",
                       f"{e}\n\nIt keeps trying. Check manually meanwhile: {START_URL}")
                state["net_error_sent"] = True

        state["seen"] = seen
        save_state(args.state_file, state)

        if args.once:
            break

        # Jitter + back off on repeated network errors, to stay polite to the server
        wait = interval * min(2 ** max(fail_streak - 1, 0), 6) if fail_streak else interval
        wait += random.uniform(0, max(10, interval * 0.2))
        if deadline and time.time() + wait > deadline:
            print(f"[{now()}] Max runtime reached, exiting.")
            break
        time.sleep(wait)


def load_state(path):
    state = {"seen": set(), "site_error_sent": False, "net_error_sent": False}
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
            state["seen"] = {dt.date.fromisoformat(d) for d in raw.get("seen", [])}
            state["site_error_sent"] = bool(raw.get("site_error_sent"))
            state["net_error_sent"] = bool(raw.get("net_error_sent"))
        except (ValueError, OSError) as e:
            print(f"[{now()}] Ignoring unreadable state file: {e}", file=sys.stderr)
    return state


def save_state(path, state):
    if not path:
        return
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "seen": sorted(d.isoformat() for d in state["seen"]),
            "site_error_sent": state["site_error_sent"],
            "net_error_sent": state["net_error_sent"],
        }, f)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
