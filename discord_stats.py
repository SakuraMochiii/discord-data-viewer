#!/usr/bin/env python3
"""Parse a Discord data package and generate an HTML analytics page."""

import json
import re
import sys
import zipfile
from collections import Counter, defaultdict
from datetime import datetime
from html import escape
from pathlib import Path


def parse_package(zip_path: str) -> dict:
    """Extract all analytics data from a Discord data package."""
    with zipfile.ZipFile(zip_path, "r") as zf:
        # Load index
        index = {}
        try:
            with zf.open("Messages/index.json") as f:
                index = json.load(f)
        except KeyError:
            pass

        # Load user info
        user = {}
        try:
            with zf.open("Account/user.json") as f:
                user = json.load(f)
        except KeyError:
            pass

        my_id = user.get("id", "")

        # Find all channel folders
        channel_dirs = set()
        for name in zf.namelist():
            if name.startswith("Messages/c") and name.endswith("/channel.json"):
                channel_dirs.add(name.split("/")[1])

        channels = []
        all_messages = []

        for cdir in sorted(channel_dirs):
            channel_id = cdir[1:]

            try:
                with zf.open(f"Messages/{cdir}/channel.json") as f:
                    meta = json.load(f)
            except (KeyError, json.JSONDecodeError):
                continue

            messages = []
            try:
                with zf.open(f"Messages/{cdir}/messages.json") as f:
                    messages = json.load(f)
            except (KeyError, json.JSONDecodeError):
                pass

            ch_type = meta.get("type", "UNKNOWN")
            guild = meta.get("guild", {})
            guild_name = guild.get("name", "") if guild else ""
            ch_name = meta.get("name", "")

            # Resolve display name from index
            idx_val = index.get(channel_id, "")
            if ch_type == "DM":
                # Extract friend name from "Direct Message with username#0"
                m = re.match(r"Direct Message with (.+?)(?:#\d+)?$", idx_val)
                friend_name = m.group(1) if m else idx_val.replace("Direct Message with ", "")
                display = friend_name
                group = "Direct Messages"
            elif ch_type == "GROUP_DM":
                display = ch_name or idx_val or "Group DM"
                group = "Group DMs"
            else:
                if " in " in idx_val:
                    parts = idx_val.split(" in ", 1)
                    display = f"#{parts[0]}"
                    group = parts[1]
                else:
                    display = f"#{ch_name}" if ch_name else f"#{channel_id}"
                    group = guild_name or "Unknown Server"

            for msg in messages:
                msg["_channel"] = display
                msg["_group"] = group
                msg["_type"] = ch_type
            all_messages.extend(messages)

            channels.append({
                "id": channel_id,
                "name": display,
                "group": group,
                "type": ch_type,
                "message_count": len(messages),
                "recipients": meta.get("recipients", []),
            })

    return {"channels": channels, "messages": all_messages, "user": user}


def compute_stats(data: dict) -> dict:
    """Compute all the analytics."""
    channels = data["channels"]
    messages = data["messages"]
    user = data["user"]

    stats = {}

    # --- Overview ---
    stats["total_messages"] = len(messages)
    stats["total_channels"] = len(channels)
    stats["total_dms"] = sum(1 for c in channels if c["type"] == "DM")
    stats["total_group_dms"] = sum(1 for c in channels if c["type"] == "GROUP_DM")
    stats["total_servers"] = len(set(
        c["group"] for c in channels
        if c["type"] not in ("DM", "GROUP_DM")
    ))

    # --- Top DM friends by message count ---
    dm_counts = []
    for c in channels:
        if c["type"] == "DM" and c["message_count"] > 0:
            dm_counts.append({"name": c["name"], "count": c["message_count"]})
    dm_counts.sort(key=lambda x: -x["count"])
    stats["top_dms"] = dm_counts[:25]

    # --- Top Group DMs ---
    gdm_counts = []
    for c in channels:
        if c["type"] == "GROUP_DM" and c["message_count"] > 0:
            gdm_counts.append({"name": c["name"], "count": c["message_count"]})
    gdm_counts.sort(key=lambda x: -x["count"])
    stats["top_group_dms"] = gdm_counts[:15]

    # --- Top servers by message count ---
    server_msgs = defaultdict(int)
    for c in channels:
        if c["type"] not in ("DM", "GROUP_DM"):
            server_msgs[c["group"]] += c["message_count"]
    stats["top_servers"] = sorted(
        [{"name": k, "count": v} for k, v in server_msgs.items() if v > 0],
        key=lambda x: -x["count"]
    )[:20]

    # --- Top channels (server channels) ---
    ch_counts = []
    for c in channels:
        if c["type"] not in ("DM", "GROUP_DM") and c["message_count"] > 0:
            ch_counts.append({
                "name": c["name"],
                "server": c["group"],
                "count": c["message_count"],
            })
    ch_counts.sort(key=lambda x: -x["count"])
    stats["top_channels"] = ch_counts[:20]

    # --- Activity over time ---
    # Messages per month
    monthly = Counter()
    hourly = Counter()
    daily_dow = Counter()
    yearly = Counter()
    dow_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    for msg in messages:
        ts = msg.get("Timestamp", "")
        if not ts:
            continue
        try:
            dt = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        monthly[dt.strftime("%Y-%m")] += 1
        hourly[dt.hour] += 1
        daily_dow[dt.weekday()] += 1
        yearly[dt.year] += 1

    stats["monthly"] = sorted(monthly.items())
    stats["hourly"] = [(h, hourly.get(h, 0)) for h in range(24)]
    stats["daily_dow"] = [(dow_names[d], daily_dow.get(d, 0)) for d in range(7)]
    stats["yearly"] = sorted(yearly.items())

    # --- Message length stats ---
    lengths = [len(msg.get("Contents", "")) for msg in messages if msg.get("Contents")]
    if lengths:
        stats["avg_msg_length"] = round(sum(lengths) / len(lengths), 1)
        stats["max_msg_length"] = max(lengths)
        stats["total_characters"] = sum(lengths)
        stats["total_words"] = sum(len(msg.get("Contents", "").split()) for msg in messages if msg.get("Contents"))
    else:
        stats["avg_msg_length"] = 0
        stats["max_msg_length"] = 0
        stats["total_characters"] = 0
        stats["total_words"] = 0

    # --- Attachment stats ---
    attachment_count = sum(1 for msg in messages if msg.get("Attachments"))
    stats["attachment_count"] = attachment_count

    # --- Top words ---
    word_counter = Counter()
    stopwords = {
        "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
        "have", "has", "had", "do", "does", "did", "will", "would", "could",
        "should", "may", "might", "shall", "can", "to", "of", "in", "for",
        "on", "with", "at", "by", "from", "as", "into", "through", "during",
        "before", "after", "and", "but", "or", "nor", "not", "so", "yet",
        "both", "either", "neither", "each", "every", "all", "any", "few",
        "more", "most", "other", "some", "such", "no", "only", "own", "same",
        "than", "too", "very", "just", "i", "me", "my", "we", "our", "you",
        "your", "he", "him", "his", "she", "her", "it", "its", "they", "them",
        "their", "what", "which", "who", "whom", "this", "that", "these",
        "those", "am", "if", "then", "else", "when", "up", "out", "about",
        "how", "why", "where", "there", "here", "also", "like", "oh", "im",
        "ok", "yeah", "dont", "thats", "lol", "omg", "u", "ur",
    }
    for msg in messages:
        text = msg.get("Contents", "")
        if not text:
            continue
        words = re.findall(r"[a-zA-Z']+", text.lower())
        for w in words:
            if len(w) >= 2 and w not in stopwords:
                word_counter[w] += 1
    stats["top_words"] = word_counter.most_common(30)

    # --- Top emoji ---
    emoji_pattern = re.compile(
        "["
        "\U0001F600-\U0001F64F"  # emoticons
        "\U0001F300-\U0001F5FF"  # symbols & pictographs
        "\U0001F680-\U0001F6FF"  # transport & map
        "\U0001F900-\U0001F9FF"  # supplemental symbols
        "\U0001FA00-\U0001FA6F"  # chess symbols
        "\U0001FA70-\U0001FAFF"  # symbols extended
        "\U00002702-\U000027B0"  # dingbats
        "\U0000FE00-\U0000FE0F"  # variation selectors
        "\U0000200D"             # zero width joiner
        "\U00002640-\U00002642"  # gender symbols
        "\U00002600-\U000026FF"  # misc symbols
        "]+",
        flags=re.UNICODE,
    )
    # Also Discord custom emoji :name:
    discord_emoji_pattern = re.compile(r"<a?:\w+:\d+>|:[a-zA-Z0-9_]+:")

    emoji_counter = Counter()
    for msg in messages:
        text = msg.get("Contents", "")
        if not text:
            continue
        for match in emoji_pattern.findall(text):
            emoji_counter[match] += 1
        for match in discord_emoji_pattern.findall(text):
            emoji_counter[match] += 1
    stats["top_emoji"] = emoji_counter.most_common(20)

    # --- Longest streak (consecutive days with messages) ---
    msg_dates = set()
    for msg in messages:
        ts = msg.get("Timestamp", "")
        if ts:
            msg_dates.add(ts[:10])
    sorted_dates = sorted(msg_dates)
    longest_streak = 0
    current_streak = 1
    streak_start = sorted_dates[0] if sorted_dates else ""
    best_streak_start = streak_start
    for i in range(1, len(sorted_dates)):
        prev = datetime.strptime(sorted_dates[i - 1], "%Y-%m-%d")
        curr = datetime.strptime(sorted_dates[i], "%Y-%m-%d")
        if (curr - prev).days == 1:
            current_streak += 1
        else:
            if current_streak > longest_streak:
                longest_streak = current_streak
                best_streak_start = streak_start
            current_streak = 1
            streak_start = sorted_dates[i]
    if current_streak > longest_streak:
        longest_streak = current_streak
        best_streak_start = streak_start
    stats["longest_streak"] = longest_streak
    stats["longest_streak_start"] = best_streak_start

    # --- First and last message dates ---
    timestamps = [msg["Timestamp"] for msg in messages if msg.get("Timestamp")]
    if timestamps:
        stats["first_message"] = min(timestamps)
        stats["last_message"] = max(timestamps)
        stats["active_days"] = len(msg_dates)
    else:
        stats["first_message"] = ""
        stats["last_message"] = ""
        stats["active_days"] = 0

    # --- Busiest day ever ---
    day_counts = Counter()
    for msg in messages:
        ts = msg.get("Timestamp", "")
        if ts:
            day_counts[ts[:10]] += 1
    if day_counts:
        busiest = day_counts.most_common(1)[0]
        stats["busiest_day"] = busiest[0]
        stats["busiest_day_count"] = busiest[1]
    else:
        stats["busiest_day"] = ""
        stats["busiest_day_count"] = 0

    # --- Night owl vs early bird ---
    night_msgs = sum(hourly.get(h, 0) for h in range(0, 6))
    morning_msgs = sum(hourly.get(h, 0) for h in range(6, 12))
    afternoon_msgs = sum(hourly.get(h, 0) for h in range(12, 18))
    evening_msgs = sum(hourly.get(h, 0) for h in range(18, 24))
    stats["time_of_day"] = {
        "night": night_msgs,
        "morning": morning_msgs,
        "afternoon": afternoon_msgs,
        "evening": evening_msgs,
    }

    return stats


def build_html(stats: dict, user: dict) -> str:
    username = user.get("global_name") or user.get("username") or "User"
    created_at = user.get("created_at", "")

    # Monthly chart data
    monthly_labels = json.dumps([m[0] for m in stats["monthly"]])
    monthly_values = json.dumps([m[1] for m in stats["monthly"]])

    # Hourly chart data
    hourly_labels = json.dumps([f"{h:02d}:00" for h, _ in stats["hourly"]])
    hourly_values = json.dumps([v for _, v in stats["hourly"]])

    # Day of week data
    dow_labels = json.dumps([d for d, _ in stats["daily_dow"]])
    dow_values = json.dumps([v for _, v in stats["daily_dow"]])

    # Top DMs bar chart — use 25 to match the table
    dm_labels = json.dumps([d["name"][:20] for d in stats["top_dms"][:25]])
    dm_values = json.dumps([d["count"] for d in stats["top_dms"][:25]])

    # Top servers bar chart
    srv_labels = json.dumps([s["name"][:25] for s in stats["top_servers"][:15]])
    srv_values = json.dumps([s["count"] for s in stats["top_servers"][:15]])

    # Time of day donut
    tod = stats["time_of_day"]
    tod_labels = json.dumps(["Night (12-6am)", "Morning (6am-12pm)", "Afternoon (12-6pm)", "Evening (6pm-12am)"])
    tod_values = json.dumps([tod["night"], tod["morning"], tod["afternoon"], tod["evening"]])

    # Top words
    top_words_html = ""
    for word, count in stats["top_words"]:
        top_words_html += f'<span class="word-chip">{escape(word)} <b>{count:,}</b></span>'

    # Top emoji
    top_emoji_html = ""
    for emoji, count in stats["top_emoji"]:
        top_emoji_html += f'<span class="emoji-chip">{escape(emoji)} <b>{count:,}</b></span>'

    # Top DMs table
    dm_table = ""
    for i, d in enumerate(stats["top_dms"][:25], 1):
        dm_table += f'<tr><td class="rank">{i}</td><td>{escape(d["name"])}</td><td class="num">{d["count"]:,}</td></tr>'

    # Top group DMs table
    gdm_table = ""
    for i, d in enumerate(stats["top_group_dms"][:15], 1):
        gdm_table += f'<tr><td class="rank">{i}</td><td>{escape(d["name"])}</td><td class="num">{d["count"]:,}</td></tr>'

    # Top channels table
    ch_table = ""
    for i, c in enumerate(stats["top_channels"][:20], 1):
        ch_table += f'<tr><td class="rank">{i}</td><td>{escape(c["name"])}</td><td class="server">{escape(c["server"])}</td><td class="num">{c["count"]:,}</td></tr>'

    # Top servers table
    srv_table = ""
    for i, s in enumerate(stats["top_servers"][:20], 1):
        srv_table += f'<tr><td class="rank">{i}</td><td>{escape(s["name"])}</td><td class="num">{s["count"]:,}</td></tr>'

    # Peak hour
    peak_hour = max(stats["hourly"], key=lambda x: x[1])
    peak_dow = max(stats["daily_dow"], key=lambda x: x[1])

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Discord Stats &mdash; {escape(username)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;800&family=DM+Sans:ital,wght@0,400;0,500;0,700;1,400&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
<style>
:root {{
  --bg-deep: #0b1120;
  --bg-mid: #111a2e;
  --glass: rgba(255,255,255,0.04);
  --glass-border: rgba(255,255,255,0.09);
  --glass-hover: rgba(255,255,255,0.07);
  --accent: #6ea8fe;
  --accent-bright: #8ec5ff;
  --accent-glow: rgba(110,168,254,0.25);
  --rose: #ff6b8a;
  --rose-glow: rgba(255,107,138,0.2);
  --amber: #fbbf24;
  --amber-glow: rgba(251,191,36,0.15);
  --teal: #2dd4bf;
  --text-primary: #eeeef4;
  --text-secondary: #8b8ba7;
  --text-dim: #5a5a78;
  --radius: 16px;
  --radius-sm: 10px;
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
html {{ scroll-behavior: smooth; }}
body {{
  font-family: "DM Sans", system-ui, sans-serif;
  background: var(--bg-deep); color: var(--text-primary); line-height: 1.6;
  min-height: 100vh;
  background-image:
    radial-gradient(ellipse 80% 60% at 20% 10%, rgba(110,168,254,0.1) 0%, transparent 60%),
    radial-gradient(ellipse 60% 50% at 80% 80%, rgba(255,107,138,0.06) 0%, transparent 50%),
    radial-gradient(ellipse 50% 40% at 50% 50%, rgba(45,212,191,0.04) 0%, transparent 50%);
  background-attachment: fixed;
}}

/* Noise overlay */
body::before {{
  content: "";
  position: fixed; inset: 0; z-index: 0; pointer-events: none;
  opacity: 0.025;
  background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E");
  background-size: 128px 128px;
}}

.container {{ max-width: 1120px; margin: 0 auto; padding: 0 24px 80px; position: relative; z-index: 1; }}

/* ---- Header ---- */
.header {{
  text-align: center; padding: 72px 24px 48px; position: relative; z-index: 1;
  margin-bottom: 40px;
}}
.header::after {{
  content: "";
  position: absolute; bottom: 0; left: 50%; transform: translateX(-50%);
  width: 120px; height: 2px;
  background: linear-gradient(90deg, transparent, var(--accent), transparent);
}}
.header h1 {{
  font-family: "Outfit", sans-serif; font-weight: 800; font-size: 38px;
  color: #fff; letter-spacing: -0.02em;
  background: linear-gradient(135deg, #fff 40%, var(--accent-bright));
  -webkit-background-clip: text; -webkit-text-fill-color: transparent;
  background-clip: text;
}}
.header .sub {{
  font-family: "DM Sans", sans-serif;
  color: var(--text-secondary); font-size: 16px; margin-top: 8px;
  letter-spacing: 0.02em;
}}

/* ---- Reveal animations ---- */
.card {{ opacity: 0; transform: translateY(24px); animation: reveal 0.6s ease forwards; }}
.card:nth-child(1) {{ animation-delay: 0.05s; }}
.card:nth-child(2) {{ animation-delay: 0.12s; }}
.card:nth-child(3) {{ animation-delay: 0.19s; }}
.card:nth-child(4) {{ animation-delay: 0.26s; }}
.card:nth-child(5) {{ animation-delay: 0.33s; }}
.card:nth-child(6) {{ animation-delay: 0.40s; }}
.card:nth-child(7) {{ animation-delay: 0.47s; }}
@keyframes reveal {{
  to {{ opacity: 1; transform: translateY(0); }}
}}

/* ---- Cards (glassmorphism) ---- */
.card {{
  background: var(--glass);
  backdrop-filter: blur(24px); -webkit-backdrop-filter: blur(24px);
  border-radius: var(--radius); padding: 28px 32px;
  margin-bottom: 24px;
  border: 1px solid var(--glass-border);
  transition: border-color 0.3s ease;
}}
.card:hover {{ border-color: rgba(255,255,255,0.13); }}
.card h2 {{
  font-family: "Outfit", sans-serif; font-weight: 600; font-size: 15px;
  text-transform: uppercase; letter-spacing: 0.12em;
  color: var(--accent); margin-bottom: 20px;
}}
.card h3 {{
  font-family: "Outfit", sans-serif; font-weight: 400; font-size: 15px;
  color: var(--text-secondary); margin-bottom: 14px; letter-spacing: 0.04em;
}}

/* ---- Stat grid ---- */
.stat-grid {{
  display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 14px;
}}
.stat-box {{
  background: rgba(255,255,255,0.025); border-radius: var(--radius-sm);
  padding: 22px 12px; text-align: center;
  border: 1px solid rgba(255,255,255,0.05);
  transition: transform 0.25s ease, box-shadow 0.25s ease;
  display: flex; flex-direction: column; align-items: center; justify-content: center;
}}
.stat-box:hover {{
  transform: translateY(-3px);
  box-shadow: 0 8px 32px rgba(110,168,254,0.1);
}}
.stat-box .num {{
  font-family: "Outfit", sans-serif; font-weight: 800;
  font-size: 28px; color: #fff; line-height: 1.1;
  white-space: nowrap;
}}
@media (min-width: 900px) {{ .stat-box .num {{ font-size: 32px; }} }}
.stat-box .label {{
  font-size: 13px; color: var(--text-dim); margin-top: 6px;
  text-transform: uppercase; letter-spacing: 0.06em; font-weight: 500;
}}

/* ---- Fun facts ---- */
.fun-facts {{
  display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 12px;
}}
.fact {{
  background: rgba(255,255,255,0.02); border-radius: var(--radius-sm);
  padding: 16px 18px; font-size: 15px;
  border-left: 3px solid var(--accent);
  transition: background 0.2s ease;
}}
.fact:hover {{ background: rgba(255,255,255,0.04); }}
.fact .val {{ color: #fff; font-weight: 700; }}

/* ---- Chart layout ---- */
.chart-row {{
  display: grid; grid-template-columns: 1fr 1fr; gap: 18px;
}}
@media (max-width: 720px) {{ .chart-row {{ grid-template-columns: 1fr; }} }}
.chart-box {{
  background: rgba(255,255,255,0.02); border-radius: var(--radius-sm);
  padding: 20px; border: 1px solid rgba(255,255,255,0.04);
  position: relative;
}}
.chart-box canvas {{ max-height: 300px; }}
.chart-wide {{ grid-column: 1 / -1; }}
.chart-wide canvas {{ max-height: 260px; }}
.chart-subtitle {{
  font-size: 13px; color: var(--text-dim); text-align: center;
  margin-bottom: 10px; letter-spacing: 0.03em;
}}

/* ---- Tables ---- */
table {{ width: 100%; border-collapse: collapse; font-size: 15px; }}
th {{
  text-align: left; color: var(--text-dim); font-weight: 600;
  padding: 10px 14px; border-bottom: 1px solid rgba(255,255,255,0.06);
  font-size: 12px; text-transform: uppercase; letter-spacing: 0.08em;
}}
td {{
  padding: 10px 14px; border-bottom: 1px solid rgba(255,255,255,0.03);
  transition: background 0.15s ease;
}}
tr:hover td {{ background: rgba(255,255,255,0.03); }}
.rank {{ color: var(--accent); font-weight: 700; width: 32px; font-family: "Outfit", sans-serif; }}
.num {{ text-align: right; font-weight: 600; color: #fff; white-space: nowrap; font-family: "Outfit", sans-serif; font-size: 15px; }}
.server {{ color: var(--text-dim); font-size: 13px; }}

/* ---- Word & emoji chips ---- */
.word-chip, .emoji-chip {{
  display: inline-block;
  background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.06);
  border-radius: 24px; padding: 6px 16px; margin: 4px; font-size: 14px;
  transition: background 0.2s ease, transform 0.15s ease;
}}
.word-chip:hover, .emoji-chip:hover {{
  background: rgba(110,168,254,0.1); transform: translateY(-1px);
}}
.word-chip b, .emoji-chip b {{ color: var(--accent); margin-left: 5px; }}
.emoji-chip {{ font-size: 18px; }}

/* ---- Two column layout ---- */
.two-col {{
  display: grid; grid-template-columns: 1fr 1fr; gap: 24px;
}}
@media (max-width: 720px) {{ .two-col {{ grid-template-columns: 1fr; }} }}

/* ---- Night owl / early bird box ---- */
.persona-box {{
  display: flex; align-items: center; justify-content: center;
  background: rgba(255,255,255,0.02); border-radius: var(--radius-sm);
  border: 1px solid rgba(255,255,255,0.04); padding: 24px;
}}
.persona-icon {{ font-size: 52px; margin-bottom: 10px; }}
.persona-label {{
  font-family: "Outfit", sans-serif; font-weight: 600;
  font-size: 22px; color: #fff;
}}
.persona-detail {{
  color: var(--text-secondary); font-size: 15px; margin-top: 6px; line-height: 1.4;
}}
</style>
</head>
<body>

<div class="header">
  <h1>{escape(username)}&rsquo;s Discord Wrapped</h1>
  <div class="sub">
    {escape(stats["first_message"][:10])} &mdash; {escape(stats["last_message"][:10])}
    &nbsp;&middot;&nbsp; {stats["active_days"]:,} active days
  </div>
</div>

<div class="container">

<!-- Overview -->
<div class="card">
  <h2>Overview</h2>
  <div class="stat-grid">
    <div class="stat-box"><div class="num">{stats["total_messages"]:,}</div><div class="label">Messages Sent</div></div>
    <div class="stat-box"><div class="num">{stats["total_words"]:,}</div><div class="label">Words Typed</div></div>
    <div class="stat-box"><div class="num">{stats["total_characters"]:,}</div><div class="label">Characters</div></div>
    <div class="stat-box"><div class="num">{stats["total_dms"]:,}</div><div class="label">DM Conversations</div></div>
    <div class="stat-box"><div class="num">{stats["total_servers"]:,}</div><div class="label">Servers</div></div>
    <div class="stat-box"><div class="num">{stats["attachment_count"]:,}</div><div class="label">Attachments</div></div>
  </div>
</div>

<!-- Fun Facts -->
<div class="card">
  <h2>Fun Facts</h2>
  <div class="fun-facts">
    <div class="fact">Busiest day: <span class="val">{escape(stats["busiest_day"])}</span> with <span class="val">{stats["busiest_day_count"]:,}</span> messages</div>
    <div class="fact">Longest daily streak: <span class="val">{stats["longest_streak"]}</span> days starting <span class="val">{escape(stats["longest_streak_start"])}</span></div>
    <div class="fact">Peak hour: <span class="val">{peak_hour[0]:02d}:00</span> ({peak_hour[1]:,} messages)</div>
    <div class="fact">Favorite day: <span class="val">{peak_dow[0]}</span> ({peak_dow[1]:,} messages)</div>
    <div class="fact">Avg message length: <span class="val">{stats["avg_msg_length"]}</span> chars</div>
    <div class="fact">Longest message: <span class="val">{stats["max_msg_length"]:,}</span> characters</div>
    <div class="fact">You&rsquo;re a <span class="val">{"Night Owl" if tod["night"] + tod["evening"] > tod["morning"] + tod["afternoon"] else "Early Bird"}</span> &mdash; {tod["night"]+tod["evening"]:,} evening/night vs {tod["morning"]+tod["afternoon"]:,} morning/afternoon messages</div>
  </div>
</div>

<!-- Activity over time -->
<div class="card">
  <h2>Activity Over Time</h2>
  <div class="chart-row">
    <div class="chart-box chart-wide">
      <div class="chart-subtitle">Messages you sent per month &mdash; only outgoing messages are included in Discord data exports</div>
      <canvas id="monthlyChart"></canvas>
    </div>
    <div class="chart-box"><canvas id="hourlyChart"></canvas></div>
    <div class="chart-box"><canvas id="dowChart"></canvas></div>
  </div>
</div>

<!-- Time of Day -->
<div class="card">
  <h2>When You Chat</h2>
  <div class="chart-row">
    <div class="chart-box"><canvas id="todChart"></canvas></div>
    <div class="persona-box">
      <div style="text-align:center;">
        <div class="persona-icon">{"&#x1F989;" if tod["night"] + tod["evening"] > tod["morning"] + tod["afternoon"] else "&#x1F426;"}</div>
        <div class="persona-label">{"Night Owl" if tod["night"] + tod["evening"] > tod["morning"] + tod["afternoon"] else "Early Bird"}</div>
        <div class="persona-detail">
          {max(tod.values()) / sum(tod.values()) * 100:.0f}% of your messages are in the
          {max(tod, key=tod.get).replace("night","late night").replace("evening","evening")} hours
        </div>
      </div>
    </div>
  </div>
</div>

<!-- Top Friends -->
<div class="card">
  <h2>Top Friends &mdash; DM messages you sent</h2>
  <div class="chart-row">
    <div class="chart-box chart-wide"><canvas id="dmChart"></canvas></div>
  </div>
  <div class="two-col" style="margin-top:20px;">
    <div>
      <h3>Top DM Conversations</h3>
      <table>
        <thead><tr><th>#</th><th>Friend</th><th style="text-align:right">Messages</th></tr></thead>
        <tbody>{dm_table}</tbody>
      </table>
    </div>
    <div>
      <h3>Top Group DMs</h3>
      <table>
        <thead><tr><th>#</th><th>Group</th><th style="text-align:right">Messages</th></tr></thead>
        <tbody>{gdm_table}</tbody>
      </table>
    </div>
  </div>
</div>

<!-- Top Servers -->
<div class="card">
  <h2>Top Servers</h2>
  <div class="chart-row">
    <div class="chart-box chart-wide"><canvas id="srvChart"></canvas></div>
  </div>
  <div class="two-col" style="margin-top:20px;">
    <div>
      <h3>By Server</h3>
      <table>
        <thead><tr><th>#</th><th>Server</th><th style="text-align:right">Messages</th></tr></thead>
        <tbody>{srv_table}</tbody>
      </table>
    </div>
    <div>
      <h3>Top Server Channels</h3>
      <table>
        <thead><tr><th>#</th><th>Channel</th><th>Server</th><th style="text-align:right">Messages</th></tr></thead>
        <tbody>{ch_table}</tbody>
      </table>
    </div>
  </div>
</div>

<!-- Words & Emoji -->
<div class="card">
  <h2>Words &amp; Emoji</h2>
  <div class="two-col">
    <div>
      <h3>Most Used Words</h3>
      <div>{top_words_html}</div>
    </div>
    <div>
      <h3>Most Used Emoji</h3>
      <div>{top_emoji_html if top_emoji_html else '<span style="color:var(--text-dim)">No emoji found</span>'}</div>
    </div>
  </div>
</div>

</div>

<script>
Chart.defaults.color = "#8b8ba7";
Chart.defaults.borderColor = "rgba(255,255,255,0.05)";
Chart.defaults.font.family = "'DM Sans', system-ui, sans-serif";
const accent = "#6ea8fe";
const accentAlpha = "rgba(110,168,254,0.25)";
const rose = "#ff6b8a";
const amber = "#fbbf24";
const teal = "#2dd4bf";
function fmtNum(v) {{ return v.toLocaleString(); }}
function widenAxis(axis) {{ if (axis.width < 58) axis.width = 58; }}
function widenLabelAxis(axis) {{ if (axis.width < 180) axis.width = 180; }}

// Monthly activity
new Chart(document.getElementById("monthlyChart"), {{
  type: "line",
  data: {{
    labels: {monthly_labels},
    datasets: [{{
      label: "Messages sent",
      data: {monthly_values},
      borderColor: accent,
      backgroundColor: (ctx) => {{
        const g = ctx.chart.ctx.createLinearGradient(0, 0, 0, ctx.chart.height);
        g.addColorStop(0, "rgba(110,168,254,0.3)");
        g.addColorStop(1, "rgba(110,168,254,0.0)");
        return g;
      }},
      fill: true, tension: 0.35, pointRadius: 0, borderWidth: 2
    }}]
  }},
  options: {{

    plugins: {{ legend: {{ display: false }} }},
    scales: {{
      x: {{ ticks: {{ maxTicksLimit: 18, font: {{ size: 12 }} }}, grid: {{ display: false }} }},
      y: {{ afterFit: widenAxis, beginAtZero: true, ticks: {{ font: {{ size: 12 }}, callback: function(v) {{ return fmtNum(v); }} }}, grid: {{ color: "rgba(255,255,255,0.03)" }} }}
    }}
  }}
}});

// Hourly activity
new Chart(document.getElementById("hourlyChart"), {{
  type: "bar",
  data: {{
    labels: {hourly_labels},
    datasets: [{{ label: "Messages", data: {hourly_values}, backgroundColor: accent, borderRadius: 6, borderSkipped: false }}]
  }},
  options: {{
    plugins: {{
      legend: {{ display: false }},
      title: {{ display: true, text: "By Hour of Day", font: {{ family: "'Outfit', sans-serif", size: 13, weight: 400 }}, padding: {{ bottom: 12 }} }}
    }},
    scales: {{
      x: {{ grid: {{ display: false }}, ticks: {{ font: {{ size: 11 }} }} }},
      y: {{ afterFit: widenAxis, beginAtZero: true, ticks: {{ font: {{ size: 12 }}, callback: function(v) {{ return fmtNum(v); }} }}, grid: {{ color: "rgba(255,255,255,0.03)" }} }}
    }}
  }}
}});

// Day of week
new Chart(document.getElementById("dowChart"), {{
  type: "bar",
  data: {{
    labels: {dow_labels},
    datasets: [{{ label: "Messages", data: {dow_values}, backgroundColor: rose, borderRadius: 6, borderSkipped: false }}]
  }},
  options: {{
    plugins: {{
      legend: {{ display: false }},
      title: {{ display: true, text: "By Day of Week", font: {{ family: "'Outfit', sans-serif", size: 13, weight: 400 }}, padding: {{ bottom: 12 }} }}
    }},
    scales: {{
      x: {{ grid: {{ display: false }}, ticks: {{ font: {{ size: 12 }} }} }},
      y: {{ afterFit: widenAxis, beginAtZero: true, ticks: {{ font: {{ size: 12 }}, callback: function(v) {{ return fmtNum(v); }} }}, grid: {{ color: "rgba(255,255,255,0.03)" }} }}
    }}
  }}
}});

// Time of day donut
new Chart(document.getElementById("todChart"), {{
  type: "doughnut",
  data: {{
    labels: {tod_labels},
    datasets: [{{
      data: {tod_values},
      backgroundColor: ["#334155", rose, amber, accent],
      borderWidth: 0,
      hoverOffset: 8
    }}]
  }},
  options: {{

    plugins: {{ legend: {{ position: "bottom", labels: {{ padding: 16, usePointStyle: true, pointStyle: "circle", font: {{ size: 13 }} }} }} }},
    cutout: "65%"
  }}
}});

// Top DMs
new Chart(document.getElementById("dmChart"), {{
  type: "bar",
  data: {{
    labels: {dm_labels},
    datasets: [{{ label: "Messages", data: {dm_values}, backgroundColor: rose, borderRadius: 6, borderSkipped: false }}]
  }},
  options: {{
    indexAxis: "y",
    layout: {{ padding: {{ right: 120 }} }},
    plugins: {{ legend: {{ display: false }} }},
    scales: {{
      x: {{ afterFit: widenAxis, beginAtZero: true, ticks: {{ font: {{ size: 12 }}, callback: function(v) {{ return fmtNum(v); }} }}, grid: {{ color: "rgba(255,255,255,0.03)" }} }},
      y: {{ afterFit: widenLabelAxis, grid: {{ display: false }}, ticks: {{ font: {{ size: 12 }} }} }}
    }}
  }}
}});

// Top Servers
new Chart(document.getElementById("srvChart"), {{
  type: "bar",
  data: {{
    labels: {srv_labels},
    datasets: [{{ label: "Messages", data: {srv_values}, backgroundColor: accent, borderRadius: 6, borderSkipped: false }}]
  }},
  options: {{
    indexAxis: "y",
    layout: {{ padding: {{ right: 120 }} }},
    plugins: {{ legend: {{ display: false }} }},
    scales: {{
      x: {{ afterFit: widenAxis, beginAtZero: true, ticks: {{ font: {{ size: 12 }}, callback: function(v) {{ return fmtNum(v); }} }}, grid: {{ color: "rgba(255,255,255,0.03)" }} }},
      y: {{ afterFit: widenLabelAxis, grid: {{ display: false }}, ticks: {{ font: {{ size: 12 }} }} }}
    }}
  }}
}});
</script>
</body>
</html>'''


def main():
    if len(sys.argv) < 2:
        print("Usage: python discord_stats.py <package.zip> [output.html]")
        sys.exit(1)

    zip_path = sys.argv[1]
    output_path = sys.argv[2] if len(sys.argv) > 2 else str(
        Path(zip_path).parent / "discord_stats.html"
    )

    if not Path(zip_path).exists():
        print(f"Error: {zip_path} not found")
        sys.exit(1)

    print(f"Reading {zip_path}...")
    data = parse_package(zip_path)
    print(f"Found {len(data['channels'])} channels, {len(data['messages']):,} messages")

    print("Computing stats...")
    stats = compute_stats(data)

    print("Generating HTML...")
    html = build_html(stats, data["user"])

    Path(output_path).write_text(html, encoding="utf-8")
    size_kb = Path(output_path).stat().st_size / 1024
    print(f"Written to {output_path} ({size_kb:.0f} KB)")
    print("Open it in a browser to view your stats!")


if __name__ == "__main__":
    main()
