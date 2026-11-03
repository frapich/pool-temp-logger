# Pool temperature logger

Every 15 minutes GitHub Actions ([log.yml](.github/workflows/log.yml)) runs `logger.py`:
it logs in to Hayward OmniLogic, reads the Oro Valley pool temperature, adds the outside
air temperature from Open-Meteo, appends the reading to `data/readings.csv` (UTC) and
publishes the page to GitHub Pages: https://frapich.github.io/pool-temp-logger/
(shown in Arizona time).

Failed readings alert the "Various" Telegram bot after two runs in a row, every 6 h while
still failing, and once on recovery (state in `data/alert_state.json`).

Secrets: `HAYWARD_USER`, `HAYWARD_PASSWORD`, `HAYWARD_MSP_ID`, `HAYWARD_BOW_ID`,
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.

History before 2026-11 comes from the old Windows logger (SQL table
`PoolMonitor.dbo.PoolWaterTempLog` on Ragnarok), converted from Eastern time to UTC.

Local test: `python logger.py --check` (reading only) or `python logger.py --build` (page only).
