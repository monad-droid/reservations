# resy-sniper

A personal, single-venue Resy release-drop sniper for a Linux server. Python 3.11+, no framework.
Dependencies: `requests`, `python-dotenv`, `PyYAML` only.

> **Warning.** Automated booking violates Resy's Terms of Service. Resy can and does deactivate
> accounts it believes are using bots, and may cancel reservations made this way. Use at your own
> risk, on your own account, for your own table. This bot never attempts to bypass CAPTCHAs, rate
> limits, or other anti-abuse measures: when it sees one it logs the raw response, notifies you, and exits.

## What it does

Restaurants on Resy release reservations a fixed number of days ahead at a fixed time of day.

* **`discover`** learns that schedule for one venue by observation and writes it to a state file.
* **`snipe`** wakes up shortly before the target date is released, polls once per second, and books
  the best available slot according to your time and table-type priorities. It fails over to the
  next-best slot if the first one is taken, stops after one successful booking, and notifies you via
  Telegram (or [ntfy](https://ntfy.sh)). Over Telegram you can also ask it `/status` or tell it `/stop`.

Helpers: **`venue`** resolves and prints the numeric `venue_id`; **`find`** does a single availability
query for a day and shows how the slots rank against your preferences.

API flow (same as the public open-source bots
[Alkaar/resy-booking-bot](https://github.com/Alkaar/resy-booking-bot) and
[jeffknaide/resy-bot](https://github.com/jeffknaide/resy-bot)):

1. `POST /4/find` JSON `{"day":"YYYY-MM-DD","lat":0,"long":0,"party_size":N,"venue_id":ID}` → available slots for a day
   (the reference bots use a GET with the same names as query params; the current resy.com client POSTs JSON)
2. `POST /3/details` JSON `{"commit":1,"config_id":"<slot config.token>","day":"…","party_size":N}` → `book_token.value`
3. `POST /3/book` (form-encoded) `book_token=…&struct_payment_method={"id":PAYMENT_METHOD_ID}&source_id=resy.com-venue-details` → confirmation

Plus, for venue lookup and window discovery: `GET /3/venue?url_slug=&location=`,
`GET /2/config?venue_id=` (reports `lead_time_in_days`), and
`GET /4/venue/calendar?venue_id=&num_seats=&start_date=&end_date=` (per-day inventory status).

## Setup

```bash
sudo useradd --system --home /opt/resy-sniper --shell /usr/sbin/nologin resy   # optional service user
sudo mkdir -p /opt/resy-sniper && sudo chown resy:resy /opt/resy-sniper
sudo -u resy git clone <this repo> /opt/resy-sniper
cd /opt/resy-sniper
sudo -u resy python3.11 -m venv .venv
sudo -u resy .venv/bin/pip install -r requirements.txt
sudo -u resy cp .env.example .env && sudo chmod 600 .env
# edit .env (credentials, below) and config.yaml (venue, party size, target date, time preferences, ntfy topic)
```

Everything is configured in `config.yaml`; credentials live only in `.env` (git-ignored).

## Extracting credentials

All three values come from the browser's developer tools while logged in to resy.com.
Open DevTools → **Network** tab, tick *Preserve log*, then browse to a restaurant page and click a
reservation time so that `/4/find`, `/3/details` and (if you complete a booking) `/3/book` show up.
Filter the list by `api.resy.com`.

| .env key | Where to find it |
| --- | --- |
| `RESY_API_KEY` | Any request to `api.resy.com` → *Request Headers* → `Authorization: ResyAPI api_key="…"`. Copy the value inside the quotes. |
| `RESY_AUTH_TOKEN` | Same request → *Request Headers* → `X-Resy-Auth-Token`. Long opaque string. It **expires**; when the bot exits with an auth error (exit code 2), log in again and re-copy it. |
| `RESY_PAYMENT_METHOD_ID` | Click *Reserve* on any slot (you can cancel afterwards, or stop at the confirmation screen — the `/3/details` response also lists `user.payment_methods[].id`). The `POST /3/book` request body (Payload tab) contains `struct_payment_method={"id":12345}`; the number is the id. |

The bot exits with a clear message if Resy answers `401` or `419` on any request.

## Finding the venue_id

The slug and location are in the restaurant's URL: `https://resy.com/cities/<location>/venues/<url_slug>`
(for Gin Gin's: `grand-rapids-mi` and `gin-gins`). Put those in `config.yaml`, then:

```bash
.venv/bin/python -m resy_sniper venue
```

prints `venue_id=…`, the venue name, `lead_time_in_days` (Resy's own statement of how many days
ahead reservations open, if the API reports it) and the venue timezone. You can paste the id into
`venue.venue_id` in `config.yaml` to skip the lookup, or leave it `null` to resolve at every start.
If the slug lookup fails the bot falls back to Resy's venue search and matches on `url_slug`.
(The id is also visible in the browser as the `venue_id` query parameter of `/4/find`.)

## Running

Sanity check parsing against a day that is currently bookable:

```bash
.venv/bin/python -m resy_sniper find --day 2026-10-02
```

### Mode 1: discover

```bash
.venv/bin/python -m resy_sniper discover
```

At startup it logs `/2/config` and `/4/venue/calendar`, then scans `/4/find` forward day by day to find
the current window length N (the last day that returns slots; sold-out and closed days also return
no slots, so Resy's `lead_time_in_days` is trusted when present and verified by observation). It then
polls `/4/find` every 60 s for `today+N+1` — and its two neighbours, so an estimate that is off by one
in either direction still catches the drop — and logs the exact timestamp the first slot appears.
After two observed daily drops (configurable) it writes `state.json`:

```json
{"venue_id": 12345, "window_days": 30, "drop_time_local": "10:00:41", "timezone": "America/Detroit",
 "observations": [...], "confirmed": true}
```

and sends a notification. A provisional state file (`"confirmed": false`) is written after the first
drop so nothing is lost if the process dies. It gives up after 48 h (configurable), notifies, exits 1.

`drop_time_local` is the *observed* time, which is up to one poll interval (60 s) after the real
release. The snipe starts polling 2 minutes before it, so this slack is covered.

### Mode 2: snipe

```bash
.venv/bin/python -m resy_sniper snipe --dry-run    # rehearse: everything except POST /3/book
.venv/bin/python -m resy_sniper snipe              # for real
# overrides if you already know the schedule:
.venv/bin/python -m resy_sniper snipe --window-days 30 --drop-time 10:00 [--target-date 2026-10-09]
```

It reads `window_days` and `drop_time_local` from the state file (CLI overrides win), works out when
the target date becomes bookable (`target − window_days` at the drop time in `America/Detroit`),
sleeps until 2 minutes before, then polls `/4/find` for the target date every 1 s (configurable,
floor 0.5 s) for up to 10 minutes after the release moment. The instant slots appear it ranks them
by your `time_preferences` (first match wins) and, within a time, by `table_types` order, then calls
`/3/details` → `/3/book`. If details or book is rejected (slot taken), it moves to the next-best
slot from the same response, then re-polls. On success it logs the confirmation, notifies, exits 0.
If nothing was booked in the window it notifies you with what it saw, then keeps checking the target
date every `snipe.watch_interval_min` minutes (default 10, `0` to disable) until the day itself, booking
the first matching cancellation. This also covers a target date that is already open but sold out.

`target.dates` may list several dates in priority order ("Friday or Saturday"): each gets its own
release-moment fast poll, open dates share the slow watch, and the first booking ends the run.
`target.mode: next_friday` picks the earliest Friday that is not yet bookable, using `window_days`.

Exit codes: 0 booked / dry run OK · 1 nothing booked / discover timed out · 2 auth (401/419) ·
3 CAPTCHA/challenge/non-JSON response · 4 config error · 5 other error.

### One command: auto

```bash
.venv/bin/python -m resy_sniper auto            # discover, then snipe, in one process
```

Runs `discover` unless `state.json` is already confirmed, then goes straight into `snipe` for the
configured target. Accepts `--dry-run` and `--target-date` like `snipe`. With Telegram configured the
process stays alive after the snipe (booked or not) and waits for `/target` or `/stop`.

### Under systemd

`resy-sniper@.service` is a template unit; the instance name is the mode.

```bash
sudo cp resy-sniper@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start resy-sniper@discover        # runs up to 48 h, exits when done
journalctl -u resy-sniper@discover -f
# once state.json exists:
sudo systemctl start resy-sniper@snipe           # sleeps until the release moment, then books, then exits
journalctl -u resy-sniper@snipe -f
```

To rehearse the snipe path under systemd: `sudo systemctl edit resy-sniper@snipe` and add
`[Service]` / `Environment=SNIPER_ARGS=--dry-run`. The unit deliberately has `Restart=no` so a
finished snipe is never re-run automatically. Adjust `User=`, `WorkingDirectory=` and the venv path
in the unit if you installed elsewhere.

## Telegram

With `notify.provider: telegram` every notification goes to your chat, and while `discover` or `snipe`
is running you can talk to it from the same chat:

| command | reply |
| --- | --- |
| `/status` | mode, venue, target, computed release moment, polls so far, last poll result, drops observed |
| `/target YYYY-MM-DD [times...]` | sets the reservation date and, optionally, the times to go after (exact `19:30` or a range `18:30-20:00`, in priority order) in `config.yaml`, then restarts the bot with it |
| `/stop` | stops the current run cleanly (exit 1, nothing booked) |
| `/help` | the list above |

Set `notify.only_when_booked: true` to receive a message only for a booking (or for a failure that
stops the bot, such as an expired token); the rest is logged only. `/status` still answers.

Messages from any other chat are ignored (and logged with their chat id). Setup:

1. In Telegram, message **@BotFather** → `/newbot` → copy the token into `.env` as `TELEGRAM_BOT_TOKEN`.
2. Open a chat with your new bot and send it any message.
3. Find your chat id: `curl -s https://api.telegram.org/bot<TOKEN>/getUpdates` → `result[0].message.chat.id`.
   Put it in `config.yaml` under `notify.telegram.chat_id`.

The listener long-polls `getUpdates` in a background thread; it does not touch the booking loop's timing.

## Logs and notifications

Every request is logged with timestamp, mode, endpoint, status, latency and (for `/4/find`) the
slots seen, to stdout and to a rotating file (`logs/resy-sniper.log`, 5 × 5 MB). 429s back off
exponentially (1 s → 60 s, honouring `Retry-After`).

Notifications go to Telegram (above) or, with `notify.provider: ntfy`, to an ntfy topic
(`notify.ntfy.topic` in `config.yaml`; pick a long random name and subscribe to it in the ntfy app).
Set `notify.provider: none` to disable.

## Tests and local rehearsal

`tests/mock_resy.py` is a small stand-in for `api.resy.com` whose responses mirror the reference
bots' fixtures. Unit tests:

```bash
python -m unittest discover -s tests -v
```

Full rehearsal of every mode without touching Resy (`RESY_API_BASE` redirects the client; it is for
testing only):

```bash
RELEASE_DAY=2026-10-07 python -m tests.mock_resy 8766 --release-in 30 &
RESY_API_BASE=http://127.0.0.1:8766 python -m resy_sniper --dry-run snipe --window-days 30 --drop-time 19:30 --target-date 2026-10-07
```

## Troubleshooting

* **401/419** → token expired: re-extract `RESY_AUTH_TOKEN`.
* **Exit 3 with an HTML body in the log** → Resy served a challenge page. Don't automate around it; log in
  from a browser and slow down.
* **`/3/book` 400 "struct_payment_method invalid"** → check the card on the account is valid and the id
  is the current one (re-extract). Alkaar's bot has the same failure mode reported by users.
* **`/3/book` 402** → the venue requires a card on file and none was sent.
* The two reference bots send `Origin: https://widgets.resy.com` on `/3/book`; this bot sends
  `https://resy.com` (what the current site uses). If Resy rejects the book POST with 400/403 and
  everything else works, that header in `resy_sniper/client.py` is the first thing to try.
