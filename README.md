# Start page

A personal widget portal in the style of iGoogle: draggable widgets for RSS feeds,
stock quotes, Hacker News, weather, notes, bookmarks and a clock. Your layout is saved to `data/layout.json`.
A daily backup is kept beside it as `data/layout.json.bak`, and is used automatically if the main file is damaged.

## Run it

Requires Python 3.10 or later.

    python -m venv .venv
    source .venv/bin/activate        # Windows: .venv\Scripts\activate
    pip install -r requirements.txt
    python app.py

Open http://localhost:5055 and set it as your browser's home page.
Use `PORT=8080 python app.py` to change the port.

## Using it

- Drag a widget by its title bar to move it between columns.
- ⚙ edits a widget (feed URL, tickers, bookmarks); ▾ collapses it; × removes it.
- "Add a widget…" in the top bar adds a new one.
- The ribbon across the top shows major indexes with sparklines. When the stock market is closed it switches to futures, and when futures are also closed it shows the last close. It refreshes every minute.
- Feeds refresh every 10 minutes, quotes every minute (cached server-side).
- "Hacker News (daily top)" groups stories by day and shows the Top 10, Top 20, Top 50% or all of
  each day's stories, using the Hacker News search API (no key needed).

## Settings

All optional, set as environment variables:

| Variable | Default | What it does |
|---|---|---|
| `PORT` | `5055` | Port to listen on |
| `HOST` | `127.0.0.1` | Address to listen on. `127.0.0.1` means this computer only; `0.0.0.0` serves your whole network |
| `START_PAGE_HOSTS` | *(empty)* | Comma-separated extra hostnames the page may be opened by, e.g. `myserver.home.arpa` |
| `AMBIENT_API_KEY`, `AMBIENT_APP_KEY` | *(none)* | Keys for the weather widget (see below) |

## Using it from other devices

By default the page only answers on this computer. To open it from phones, tablets or other PCs:

    HOST=0.0.0.0 python app.py

then browse to `http://<this computer's IP>:5055`. To use a name instead of the IP, add the name to
`START_PAGE_HOSTS`. `localhost` and this computer's own hostname are always allowed. Requests under any
other name are refused, which blocks DNS-rebinding attacks.

**There is no login.** Anyone who can reach the port can read and change your layout, including your
notes and bookmarks. Only serve it on a network you trust, and preferably use a firewall to limit which
devices can connect. For example, with `ufw`:

    sudo ufw allow from 192.168.1.0/24 to any port 5055 proto tcp

Built-in protections, which need no setup:

- The feed widget won't fetch addresses on your private network or this machine, so it can't be used
  to probe your router or other devices.
- Requests from other websites can't change your layout.
- Request sizes are capped, and the page is sent with strict security headers.

## Run it as a service (Linux)

`deploy/start-page.service` is a systemd unit with sandboxing: it can only write its own `data/`
folder. It is set up for the author's machine, so before using it, edit `User=`, `Group=`,
`WorkingDirectory=`, `ExecStart=`, `ReadWritePaths=` and the `Environment=` lines. Then:

    sudo cp deploy/start-page.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now start-page

On Windows use Task Scheduler, and on macOS a LaunchAgent, to run `python app.py` from this folder at login.

## Weather widget (Ambient Weather station)

Optional. Without keys the widget shows a "not configured" message.

1. Sign in at https://ambientweather.net/account and create an **API Key** and an **Application Key**.
2. Put them in `data/ambient.json` (see `static/ambient.example.json`; the `data/` folder is git-ignored):

       {"api_key": "YOUR_API_KEY", "app_key": "YOUR_APP_KEY"}

   On Linux, make it private with `chmod 600 data/ambient.json`. You can use the environment variables
   `AMBIENT_API_KEY` and `AMBIENT_APP_KEY` instead, but don't put keys in a systemd unit file, which
   any user on the machine can read.
3. Restart the app, then choose "Weather (Ambient station)" under "Add a widget…".

It shows the first station on the account. Keep the keys private, and never commit them.

## Notes

- Quotes come from Yahoo Finance through `yfinance`. They can be delayed, and the library
  occasionally breaks when Yahoo changes things; `pip install -U yfinance` usually fixes it.
- The app runs on the `waitress` server. If `waitress` isn't installed, it falls back to Flask's
  built-in development server, which is fine on your own computer.
