#!/usr/bin/env python3
"""
Retirement / walkover / forfeit alerts -> Telegram.

Sources (free):
  * Tennis  : ESPN public scoreboard API (ATP + WTA), no key needed
  * Esports : PandaScore free "fixtures" plan (CS2, LoL, Dota 2, Valorant, ...), free key

Environment variables:
  TELEGRAM_BOT_TOKEN   required  (from @BotFather)
  TELEGRAM_CHAT_ID     required  (your user id, or a group/channel id)
  PANDASCORE_TOKEN     optional  (esports is skipped if missing)
  STATE_FILE           optional  (default: seen.json)

Usage:
  python alerter.py --test        send a test message to Telegram
  python alerter.py               check once and exit (for cron / GitHub Actions)
  python alerter.py --loop 60     check every 60 seconds forever
  python alerter.py --dry-run     print alerts instead of sending them

On the very first run (no state file yet) existing matches are recorded silently,
so you don't get flooded with old results. Use --alert-existing to override.
"""

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

ESPN_URL = "https://site.api.espn.com/apis/site/v2/sports/tennis/{tour}/scoreboard"
PANDA_URL = "https://api.pandascore.co/matches"
UA = {"User-Agent": "ret-wo-alerter/1.0"}

STATE_FILE = os.environ.get("STATE_FILE", "seen.json")
STATE_KEEP_DAYS = 14


# ---------------------------------------------------------------- helpers

def http_json(url, headers=None, timeout=20):
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def esc(s):
    return html.escape(str(s if s is not None else ""), quote=False)


def classify(text):
    """Map free text from a status / result note to an outcome, or None."""
    t = (text or "").lower()
    if "walkover" in t or re.search(r"\bw/?o\b", t):
        return "Walkover"
    if "retir" in t or re.search(r"\bret\b\.?", t):
        return "Retired"
    if "default" in t or "disqualif" in t:
        return "Defaulted"
    return None


# ---------------------------------------------------------------- tennis

def _player_name(c):
    for key in ("athlete", "roster", "team"):
        obj = c.get(key)
        if isinstance(obj, dict) and obj.get("displayName"):
            return obj["displayName"]
    ath = c.get("athletes")
    if isinstance(ath, list) and ath:
        return " / ".join(a.get("displayName", "?") for a in ath)
    return c.get("displayName") or "?"


def _score_line(competitors):
    sets = []
    lines = [c.get("linescores") or [] for c in competitors]
    if len(lines) != 2:
        return ""
    for a, b in zip(lines[0], lines[1]):
        av, bv = a.get("value"), b.get("value")
        if av is None or bv is None:
            continue
        sets.append(f"{int(av)}-{int(bv)}")
    return " ".join(sets)


def parse_espn(data, tour):
    """Yield alert dicts for walkovers in an ESPN scoreboard payload (retirements/defaults skipped)."""
    for event in data.get("events", []) or []:
        tournament = event.get("name") or event.get("shortName") or "Tournament"
        for group in event.get("groupings", []) or []:
            draw = (group.get("grouping") or {}).get("displayName", "")
            for comp in group.get("competitions", []) or []:
                st = (comp.get("status") or {}).get("type") or {}
                notes = " ".join(n.get("text", "") for n in comp.get("notes") or [])
                outcome = None
                for text in (st.get("description"), st.get("name"), st.get("detail"),
                             st.get("shortDetail"), notes):
                    outcome = classify(text)
                    if outcome:
                        break
                if outcome != "Walkover":  # tennis: walkovers only (no retirements/defaults)
                    continue
                comps = comp.get("competitors") or []
                names = [_player_name(c) for c in comps]
                winner = next((_player_name(c) for c in comps if c.get("winner")), None)
                yield {
                    "id": f"tennis:{tour}:{comp.get('id')}",
                    "sport": f"🎾 {tour.upper()}",
                    "outcome": outcome,
                    "event": f"{tournament} – {draw}".strip(" –"),
                    "round": (comp.get("round") or {}).get("displayName", ""),
                    "match": " vs ".join(names),
                    "winner": winner,
                    "score": "",
                    "note": notes.strip(),
                    "times": [("Scheduled", comp.get("date") or comp.get("startDate"))],
                }


def fetch_tennis():
    alerts = []
    today = datetime.now(timezone.utc)
    dates = [None, (today - timedelta(days=1)).strftime("%Y%m%d")]  # current + yesterday (late finishes)
    for tour in ("atp", "wta"):
        for d in dates:
            url = ESPN_URL.format(tour=tour) + (f"?dates={d}" if d else "")
            try:
                alerts.extend(parse_espn(http_json(url), tour))
            except Exception as e:
                print(f"[warn] ESPN {tour} {d or 'today'}: {e}", file=sys.stderr)
    return alerts


# ---------------------------------------------------------------- esports

def parse_panda(matches):
    """Yield alerts for canceled matches (incl. forfeits) and series abandoned mid-way.
    Postponed / rescheduled matches are ignored. PandaScore has no separate 'abandoned'
    status: a match stopped after it began shows as canceled with a start time, or as a
    series with forfeited games."""
    for m in matches or []:
        opp = [((o or {}).get("opponent") or {}) for o in m.get("opponents") or []]
        names = {o.get("id"): o.get("name") or o.get("acronym") or "?" for o in opp}
        game_name = ((m.get("videogame") or {}).get("name")) or "Esports"
        league = ((m.get("league") or {}).get("name")) or ""
        serie = ((m.get("serie") or {}).get("full_name")) or ""
        tourn = ((m.get("tournament") or {}).get("name")) or ""
        event = " – ".join(x for x in (league, serie, tourn) if x)
        winner = names.get(m.get("winner_id"))
        score = " - ".join(str(r.get("score")) for r in m.get("results") or [])
        base = {
            "sport": f"🎮 {game_name}",
            "event": event,
            "round": "",
            "match": " vs ".join(names.values()) or m.get("name", "?"),
            "winner": winner,
            "note": "",
            "times": [("Scheduled", m.get("original_scheduled_at") or m.get("scheduled_at")),
                      ("Started", m.get("begin_at")),
                      ("Updated", m.get("modified_at"))],
        }
        status = m.get("status")
        if status == "postponed":
            continue
        if status == "canceled":
            if m.get("forfeit"):
                outcome, note = "Canceled – forfeit", "Win awarded by forfeit"
            elif m.get("begin_at"):
                outcome, note = "Abandoned", "Match had started before it was canceled"
            else:
                outcome, note = "Canceled", ""
            yield {**base, "id": f"esports:{m.get('id')}", "outcome": outcome,
                   "score": score if outcome == "Abandoned" else "", "note": note}
            continue
        forfeited_games = [g for g in m.get("games") or [] if g.get("forfeit")]
        if forfeited_games:
            nums = ", ".join(str(g.get("position", "?")) for g in forfeited_games)
            yield {**base, "id": f"esports:{m.get('id')}:games",
                   "outcome": "Abandoned mid-series", "score": score,
                   "note": f"Forfeited game(s): {nums}"}


def fetch_esports():
    token = os.environ.get("PANDASCORE_TOKEN")
    if not token:
        return []
    alerts = []
    # Most recently modified matches across all games; a forfeit shows up as a modification.
    for page in (1, 2):
        q = urllib.parse.urlencode({"sort": "-modified_at", "per_page": 100, "page": page})
        try:
            data = http_json(f"{PANDA_URL}?{q}", {"Authorization": f"Bearer {token}"})
            alerts.extend(parse_panda(data))
        except Exception as e:
            print(f"[warn] PandaScore page {page}: {e}", file=sys.stderr)
            break
    return alerts


# ---------------------------------------------------------------- telegram

def _local_tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(os.environ.get("TIMEZONE", "Europe/Athens"))
    except Exception:
        return timezone.utc


def fmt_time(value):
    """ISO string from the APIs -> 'Thu 5 Nov, 14:30' in local time (default Europe/Athens)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(_local_tz())
        return f"{dt:%a} {dt.day} {dt:%b}, {dt:%H:%M}"
    except Exception:
        return None


def format_alert(a):
    lines = [f"<b>{esc(a['outcome'].upper())}</b> · {esc(a['sport'])}",
             f"<b>{esc(a['match'])}</b>"]
    if a.get("event"):
        lines.append(esc(a["event"]) + (f" · {esc(a['round'])}" if a.get("round") else ""))
    if a.get("score"):
        lines.append(f"Score: {esc(a['score'])}")
    if a.get("winner"):
        lines.append(f"Advances/wins: {esc(a['winner'])}")
    if a.get("note"):
        lines.append(f"<i>{esc(a['note'])}</i>")
    labels = {"Scheduled": "🕒 Scheduled", "Started": "▶️ Started", "Updated": "📝 Status changed"}
    for label, value in a.get("times") or []:
        t = fmt_time(value)
        if t:
            lines.append(f"{labels.get(label, label)}: {t}")
    lines.append(f"🔔 Detected: {fmt_time(datetime.now(timezone.utc).isoformat())}")
    return "\n".join(lines)


def send_telegram(text):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat = os.environ["TELEGRAM_CHAT_ID"].strip()
    body = urllib.parse.urlencode({"chat_id": chat, "text": text, "parse_mode": "HTML",
                                   "disable_web_page_preview": "true"}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token.strip()}/sendMessage", data=body, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            desc = json.loads(e.read().decode()).get("description", "")
        except Exception:
            desc = ""
        hint = {401: "TELEGRAM_BOT_TOKEN secret is wrong",
                400: "TELEGRAM_CHAT_ID secret is wrong (or you haven't pressed Start in the bot)",
                403: "the bot can't message you: open it in Telegram and press Start"}.get(e.code, "")
        raise RuntimeError(f"Telegram error {e.code}: {desc}" + (f" -> {hint}" if hint else "")) from None


# ---------------------------------------------------------------- state + main

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f), True
    except FileNotFoundError:
        return {}, False


def save_state(state):
    cutoff = time.time() - STATE_KEEP_DAYS * 86400
    state = {k: v for k, v in state.items() if v >= cutoff}
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


def check_once(dry_run=False, alert_existing=False):
    state, existed = load_state()
    silent = not existed and not alert_existing
    alerts = fetch_tennis() + fetch_esports()
    new = 0
    for a in alerts:
        if a["id"] in state:
            continue
        if not silent:
            msg = format_alert(a)
            if dry_run:
                print(msg + "\n---")
            else:
                try:
                    send_telegram(msg)
                except Exception as e:
                    print(f"[warn] Telegram send failed, will retry next run: {e}", file=sys.stderr)
                    continue
            new += 1
        state[a["id"]] = time.time()
    save_state(state)
    if silent:
        print(f"First run: recorded {len(alerts)} existing results silently. Alerts start from the next check.")
    else:
        print(f"{datetime.now():%H:%M:%S} checked {len(alerts)} flagged matches, {new} new alert(s).")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--loop", type=int, metavar="SECONDS", help="poll forever at this interval")
    p.add_argument("--dry-run", action="store_true", help="print alerts instead of sending")
    p.add_argument("--alert-existing", action="store_true", help="on first run, alert on existing results too")
    p.add_argument("--test", action="store_true", help="send a test Telegram message and exit")
    args = p.parse_args()

    if args.test:
        try:
            send_telegram("✅ Walkover / canceled alerter is connected.")
        except Exception as e:
            sys.exit(f"TEST FAILED: {e}")
        print("Test message sent.")
        return
    if not args.dry_run and not (os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")):
        sys.exit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (or use --dry-run).")

    if args.loop:
        interval = max(30, args.loop)
        while True:
            try:
                check_once(args.dry_run, args.alert_existing)
            except Exception as e:
                print(f"[error] {e}", file=sys.stderr)
            time.sleep(interval)
    else:
        check_once(args.dry_run, args.alert_existing)


if __name__ == "__main__":
    main()
