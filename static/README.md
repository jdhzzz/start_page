# Start page

A personal widget portal in the style of iGoogle: draggable widgets for RSS feeds,
stock quotes, notes, bookmarks and a clock. Layout is saved to `data/layout.json`.

## Run it

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

## Notes

- Quotes come from Yahoo Finance through `yfinance`. They can be delayed and the
  library occasionally breaks when Yahoo changes things; `pip install -U yfinance` usually fixes it.
- The server only listens on 127.0.0.1, so it's reachable from this computer only.
- To start it at login: Task Scheduler (Windows), a LaunchAgent (macOS), or a
  systemd user service (Linux) that runs `python app.py` from this folder.

## Weather widget (Ambient Weather station)

1. Sign in at https://ambientweather.net/account and create an **API Key** and an **Application Key**.
2. Give them to the app, either as environment variables `AMBIENT_API_KEY` and `AMBIENT_APP_KEY`,
   or in `data/ambient.json` (see `ambient.example.json`; the `data/` folder is git-ignored):

       {"api_key": "YOUR_API_KEY", "app_key": "YOUR_APP_KEY"}

   (With the systemd service, add `Environment=AMBIENT_API_KEY=...` lines instead.)
3. Restart the app, then choose "Weather (Ambient station)" under "Add a widget…".

It shows the first station on the account. Keep the keys private; don't publish them or commit them.

## Hacker News by day

"Hacker News (daily top)" groups stories by day and shows the Top 10, Top 20, Top 50% or all of
each day's stories, using the Hacker News search API (no key). It needs `hn.py` registered in `app.py`:

    from hn import bp as hn_bp
    app.register_blueprint(hn_bp)
