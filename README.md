# Discord Data Stats Analyzer

Generate a visual analytics page from your Discord data export package.

![Python](https://img.shields.io/badge/python-3.8+-blue)

## Features

- **Overview stats** — total messages, words, characters, DMs, servers, attachments
- **Fun facts** — busiest day, longest daily streak, peak hour, night owl vs early bird
- **Activity charts** — messages over time, hour of day, day of week, time-of-day breakdown
- **Top friends** — ranked DM conversations and group DMs by message count
- **Top servers** — ranked servers and channels by message count
- **Words & emoji** — most frequently used words and emoji

## Usage

### 1. Request your Discord data

1. Open Discord → Settings → Privacy & Safety → **Request all of my Data**
2. Wait for the email with your download link (can take up to 30 days)
3. Download the `package.zip`

### 2. Generate your stats

```bash
python3 discord_stats.py package.zip
```

This outputs a `discord_stats.html` file in the same directory.

You can also specify a custom output path:

```bash
python3 discord_stats.py package.zip my_stats.html
```

### 3. View

Open `discord_stats.html` in any browser. It's a single self-contained file (uses [Chart.js](https://www.chartjs.org/) via CDN for charts).

## Requirements

- Python 3.8+
- No external dependencies (stdlib only)
- Internet connection to load Chart.js when viewing the HTML

## Notes

- The data export only includes messages **you** sent, so all counts reflect your outgoing messages
- No data is uploaded anywhere — everything runs locally and the HTML is self-contained
- Your `package.zip` is gitignored and should never be committed
