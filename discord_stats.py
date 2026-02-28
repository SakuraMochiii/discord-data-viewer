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

    # Top DMs bar chart
    dm_labels = json.dumps([d["name"][:20] for d in stats["top_dms"][:15]])
    dm_values = json.dumps([d["count"] for d in stats["top_dms"][:15]])

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
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Noto Sans", Ubuntu, sans-serif;
  background: #1a1a2e; color: #e0e0e0; line-height: 1.5;
}}
.container {{ max-width: 1100px; margin: 0 auto; padding: 24px 20px 60px; }}

/* Header */
.header {{
  text-align: center; padding: 48px 0 32px;
  background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%);
  border-bottom: 2px solid #5865f2;
  margin-bottom: 32px;
}}
.header h1 {{ font-size: 28px; color: #fff; margin-bottom: 4px; }}
.header .sub {{ color: #949ba4; font-size: 14px; }}

/* Cards */
.card {{
  background: #222244; border-radius: 12px; padding: 24px;
  margin-bottom: 20px; border: 1px solid #333366;
}}
.card h2 {{
  font-size: 16px; text-transform: uppercase; letter-spacing: 0.05em;
  color: #5865f2; margin-bottom: 16px; font-weight: 700;
}}
.card h3 {{
  font-size: 14px; color: #949ba4; margin-bottom: 12px; font-weight: 600;
}}

/* Stat grid */
.stat-grid {{
  display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 16px; margin-bottom: 8px;
}}
.stat-box {{
  background: #1a1a3a; border-radius: 10px; padding: 16px; text-align: center;
  border: 1px solid #2a2a5a;
}}
.stat-box .num {{ font-size: 28px; font-weight: 700; color: #fff; }}
.stat-box .label {{ font-size: 12px; color: #949ba4; margin-top: 2px; }}

/* Fun facts */
.fun-facts {{
  display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 12px;
}}
.fact {{
  background: #1a1a3a; border-radius: 8px; padding: 14px 16px;
  border-left: 3px solid #5865f2; font-size: 14px;
}}
.fact .val {{ color: #fff; font-weight: 600; }}

/* Charts */
.chart-row {{
  display: grid; grid-template-columns: 1fr 1fr; gap: 20px;
}}
@media (max-width: 700px) {{ .chart-row {{ grid-template-columns: 1fr; }} }}
.chart-box {{
  background: #1a1a3a; border-radius: 10px; padding: 16px;
  border: 1px solid #2a2a5a; position: relative;
}}
.chart-box canvas {{ max-height: 300px; }}
.chart-wide {{ grid-column: 1 / -1; }}
.chart-wide canvas {{ max-height: 250px; }}

/* Tables */
table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
th {{ text-align: left; color: #949ba4; font-weight: 600; padding: 8px 12px; border-bottom: 1px solid #333366; font-size: 12px; text-transform: uppercase; }}
td {{ padding: 8px 12px; border-bottom: 1px solid #2a2a4a; }}
tr:hover {{ background: #2a2a4a; }}
.rank {{ color: #5865f2; font-weight: 700; width: 30px; }}
.num {{ text-align: right; font-weight: 600; color: #fff; white-space: nowrap; }}
.server {{ color: #949ba4; font-size: 12px; }}

/* Tags */
.word-chip, .emoji-chip {{
  display: inline-block; background: #1a1a3a; border: 1px solid #2a2a5a;
  border-radius: 20px; padding: 4px 12px; margin: 3px; font-size: 13px;
}}
.word-chip b, .emoji-chip b {{ color: #5865f2; margin-left: 4px; }}
.emoji-chip {{ font-size: 16px; }}

/* Two column layout */
.two-col {{
  display: grid; grid-template-columns: 1fr 1fr; gap: 20px;
}}
@media (max-width: 700px) {{ .two-col {{ grid-template-columns: 1fr; }} }}
</style>
</head>
<body>

<div class="header">
  <h1>{escape(username)}&rsquo;s Discord Wrapped</h1>
  <div class="sub">
    {escape(stats["first_message"][:10])} &mdash; {escape(stats["last_message"][:10])}
    &middot; {stats["active_days"]:,} active days
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
    <div class="fact">You&rsquo;re a <span class="val">{"Night Owl 🦉" if tod["night"] + tod["evening"] > tod["morning"] + tod["afternoon"] else "Early Bird 🐦"}</span> &mdash; {tod["night"]+tod["evening"]:,} evening/night vs {tod["morning"]+tod["afternoon"]:,} morning/afternoon messages</div>
  </div>
</div>

<!-- Activity over time -->
<div class="card">
  <h2>Activity Over Time</h2>
  <div class="chart-row">
    <div class="chart-box chart-wide"><canvas id="monthlyChart"></canvas></div>
    <div class="chart-box"><canvas id="hourlyChart"></canvas></div>
    <div class="chart-box"><canvas id="dowChart"></canvas></div>
  </div>
</div>

<!-- Time of Day -->
<div class="card">
  <h2>When You Chat</h2>
  <div class="chart-row">
    <div class="chart-box"><canvas id="todChart"></canvas></div>
    <div class="chart-box" style="display:flex;align-items:center;justify-content:center;">
      <div style="text-align:center;">
        <div style="font-size:48px;margin-bottom:8px;">{"🦉" if tod["night"] + tod["evening"] > tod["morning"] + tod["afternoon"] else "🐦"}</div>
        <div style="font-size:20px;font-weight:700;color:#fff;">{"Night Owl" if tod["night"] + tod["evening"] > tod["morning"] + tod["afternoon"] else "Early Bird"}</div>
        <div style="color:#949ba4;font-size:13px;margin-top:4px;">
          {max(tod.values()) / sum(tod.values()) * 100:.0f}% of your messages are in the
          {max(tod, key=tod.get).replace("night","late night").replace("evening","evening")} hours
        </div>
      </div>
    </div>
  </div>
</div>

<!-- Top Friends -->
<div class="card">
  <h2>Top Friends (by DM messages you sent)</h2>
  <div class="chart-row">
    <div class="chart-box chart-wide"><canvas id="dmChart"></canvas></div>
  </div>
  <div class="two-col" style="margin-top:16px;">
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
  <div class="two-col" style="margin-top:16px;">
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
      <div>{top_emoji_html if top_emoji_html else '<span style="color:#949ba4">No emoji found</span>'}</div>
    </div>
  </div>
</div>

</div>

<script>
Chart.defaults.color = "#949ba4";
Chart.defaults.borderColor = "#2a2a5a";
const purple = "#5865f2";
const purpleAlpha = "rgba(88,101,242,0.3)";

// Monthly activity
new Chart(document.getElementById("monthlyChart"), {{
  type: "line",
  data: {{
    labels: {monthly_labels},
    datasets: [{{ label: "Messages", data: {monthly_values}, borderColor: purple, backgroundColor: purpleAlpha, fill: true, tension: 0.3, pointRadius: 0 }}]
  }},
  options: {{
    plugins: {{ legend: {{ display: false }} }},
    scales: {{
      x: {{ ticks: {{ maxTicksLimit: 20 }} }},
      y: {{ beginAtZero: true }}
    }}
  }}
}});

// Hourly activity
new Chart(document.getElementById("hourlyChart"), {{
  type: "bar",
  data: {{
    labels: {hourly_labels},
    datasets: [{{ label: "Messages", data: {hourly_values}, backgroundColor: purple, borderRadius: 4 }}]
  }},
  options: {{
    plugins: {{ legend: {{ display: false }}, title: {{ display: true, text: "By Hour of Day" }} }},
    scales: {{ y: {{ beginAtZero: true }} }}
  }}
}});

// Day of week
new Chart(document.getElementById("dowChart"), {{
  type: "bar",
  data: {{
    labels: {dow_labels},
    datasets: [{{ label: "Messages", data: {dow_values}, backgroundColor: "#e94560", borderRadius: 4 }}]
  }},
  options: {{
    plugins: {{ legend: {{ display: false }}, title: {{ display: true, text: "By Day of Week" }} }},
    scales: {{ y: {{ beginAtZero: true }} }}
  }}
}});

// Time of day donut
new Chart(document.getElementById("todChart"), {{
  type: "doughnut",
  data: {{
    labels: {tod_labels},
    datasets: [{{ data: {tod_values}, backgroundColor: ["#16213e","#e94560","#f5a623","#5865f2"], borderWidth: 0 }}]
  }},
  options: {{
    plugins: {{ legend: {{ position: "bottom" }} }},
    cutout: "60%"
  }}
}});

// Top DMs
new Chart(document.getElementById("dmChart"), {{
  type: "bar",
  data: {{
    labels: {dm_labels},
    datasets: [{{ label: "Messages", data: {dm_values}, backgroundColor: "#e94560", borderRadius: 4 }}]
  }},
  options: {{
    indexAxis: "y",
    plugins: {{ legend: {{ display: false }} }},
    scales: {{ x: {{ beginAtZero: true }} }}
  }}
}});

// Top Servers
new Chart(document.getElementById("srvChart"), {{
  type: "bar",
  data: {{
    labels: {srv_labels},
    datasets: [{{ label: "Messages", data: {srv_values}, backgroundColor: purple, borderRadius: 4 }}]
  }},
  options: {{
    indexAxis: "y",
    plugins: {{ legend: {{ display: false }} }},
    scales: {{ x: {{ beginAtZero: true }} }}
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
