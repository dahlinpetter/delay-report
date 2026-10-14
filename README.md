# Öresund delay report

Logs every Öresundståg delay between your stations during commute hours, and every Friday emails each commuter a report: which of their usual trains were 20+ minutes late, and roughly how much they can claim from Skånetrafiken.

```
GitHub Actions (every 10 min, Mon–Fri)        GitHub Actions (Fridays)
  collect.py  ──►  data/observations/*.json  ──►  report.py  ──►  email
        ▲
  Trafiklab realtime API (free, CC BY 4.0)
```

No server and no cost: everything runs on GitHub's free scheduled jobs.

## Setup (about 15 minutes)

1. **Create a private GitHub repo** named `delay-report` and upload all files from this folder (including the hidden `.github` folder). Keep it **private**: `config.json` holds commuters' email addresses.

2. **Add secrets.** In the repo, go to Settings → Secrets and variables → Actions → New repository secret:

   | Name | Value |
   |---|---|
   | `TRAFIKLAB_KEY` | your Trafiklab API key |
   | `SMTP_HOST` | `smtp.gmail.com` |
   | `SMTP_PORT` | `465` |
   | `SMTP_USER` | the Gmail address that sends the reports |
   | `SMTP_PASS` | a Gmail **app password** (Google Account → Security → 2-Step Verification → App passwords), not your normal password |
   | `MAIL_FROM` | e.g. `Delay Report <you@gmail.com>` |

3. **Edit `config.json`.** Put your email in `"email"`, and set your ticket price, time windows and usual departure times. Add more commuters by copying the user block.

4. **Start the collector.** Go to the Actions tab, open "Collect train delays" and click **Run workflow**. Check the log: it should print `Resolved 'København H' -> ...` and a number of stored observations. From then on it runs automatically every 10 minutes on weekdays.

5. **Test the email.** On Friday it sends automatically. To test earlier, run "Weekly delay email" manually from the Actions tab.

## Things to verify

- **Danish side:** after the first runs, open `data/observations/YYYY-MM.json` and check that København H arrivals have `"is_realtime": true` and non-zero `delay_s` values on some trains. If they're all 0, Trafiklab doesn't carry Danish real-time data, and we'll need to add DSB/Rejseplanen data.
- **Station lookup:** `data/stations.json` shows which stop each name matched. If it's wrong, set the right id in `config.json` → `station_ids`.
- **Trip value for period tickets:** the report values one trip at `price_sek / trips_factor` (default 40). Check which factor Skånetrafiken actually uses and update it.

## Files

- `collect.py`: fetches departure and arrival boards and keeps the latest delay per train
- `report.py`: matches each commuter's trains, applies 50/75/100% at 20/40/60 minutes, and renders and sends the email. Run `python report.py --dry-run` to only write `reports/*.html`.
- `tools/simulate.py`: offline test with a fake week of traffic
- Data from Trafiklab.se, licensed CC BY 4.0. The attribution is included in every email.
