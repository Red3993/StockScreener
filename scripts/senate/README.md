# Senate trades (runs on your computer)

The Senate's disclosure site blocks cloud servers, so this one piece runs on your own computer once a day.
It reads every Senate periodic transaction report from the last 3 years (first run takes ~15–20 minutes,
later runs a minute or two), saves `senate.json`, and uploads it to the `senate-data` branch of the repo.
The nightly GitHub job merges it with the House trades.

Your computer has to be on and online at 6:15pm for the daily run; if it's asleep, the run happens the next time.

## 1. Get the files
Download this folder from GitHub: open https://github.com/Red3993/StockScreener → **Code** → **Download ZIP**,
unzip it, and open `scripts/senate`.

## 2. Make an upload token (once, ~2 minutes)
1. Go to https://github.com/settings/personal-access-tokens/new
2. Name: `StockScreener Senate`. Expiration: 1 year.
3. Repository access: **Only select repositories** → `Red3993/StockScreener`.
4. Permissions → Repository permissions → **Contents: Read and write**.
5. **Generate token** and copy it.
6. Save it to a file named `.stockscreener_token` in your home folder:
   - Mac (Terminal): `echo "PASTE_TOKEN_HERE" > ~/.stockscreener_token && chmod 600 ~/.stockscreener_token`
   - Windows (PowerShell): `Set-Content -Path "$HOME\.stockscreener_token" -Value "PASTE_TOKEN_HERE"`

Don't paste the token into chat or anywhere else.

## 3. Test, then run and schedule
Mac (Terminal, in this folder):
```
python3 senate_trades.py --test      # parses 3 recent reports and prints them; saves nothing
python3 senate_trades.py             # first full run (3 years), uploads senate.json
bash setup_mac.sh                    # schedules it daily at 6:15pm
```
Windows (Command Prompt, in this folder; install Python from python.org first if `py` isn't found):
```
py senate_trades.py --test
py senate_trades.py
setup_windows.bat
```

## Troubleshooting
- **"returned 403 (blocked)"**: you're on a VPN or a network the Senate site blocks. Turn the VPN off.
- **"Upload failed: HTTP 401/403"**: the token is wrong, expired, or lacks Contents: Read and write on the repo.
- Scanned paper reports can't be read and are counted as skipped.
