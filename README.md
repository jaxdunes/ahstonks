# AH Ticker: Normal Horde US

A stock-market style view of **every item** on the WoW: Forever **Normal Horde US** auction
house, using Blizzard's own Game Data API.

Blizzard only returns what's listed right now, so the fetcher keeps its own price history:
every hourly run adds one snapshot. Charts fill in as history builds up (1D needs about a
day, 1W a week, and so on).

## Files

- `index.html`: the whole site, no build step. Shows demo data if `data.json` is missing.
- `privacy.html`: the privacy policy Blizzard's API terms require.
- `config.json`: realm settings. `house_id` is already set to **114** (Normal Horde US).
- `fetch_data.py`: takes a snapshot and rebuilds the site data. Python 3 only, no packages.
- `.github/workflows/update-data.yml`: runs the fetcher every hour and publishes the site.

Files the fetcher creates (ignored by git):
- `data.json`: one row per item for the market list.
- `items/<id>.json`: one item's full history, loaded when you click it.
- `history.json.gz`, `items_meta.json.gz`: saved price history and item names.

## Run it on your Mac

```
cd ~/Downloads/ah-ticker
read -s BLIZZARD_CLIENT_ID && export BLIZZARD_CLIENT_ID
read -s BLIZZARD_CLIENT_SECRET && export BLIZZARD_CLIENT_SECRET
python3 fetch_data.py
python3 -m http.server 8000
```
Then open http://localhost:8000. (Paste your Client ID / Secret after each `read -s` line and press Return.)

The first run also looks up names and categories for every item, so it takes longer than later runs.

## Using the site

- **Search** by item name.
- **Category** dropdown: pick a whole category (Trade Goods, Armor...) or a subcategory (Cloth, Herb...).
- **Sort**: grouped by category, most listed, biggest gainers/losers, price, or name.
- **★ Favorites**: star any item. Favorites only filter, and the ticker tape shows your favorites.
  Saved in your browser only.
- **In stock**: hides items with nothing listed right now.
- Top gainers/losers only count items with 10+ listed, so one weird listing can't top the chart.
- Every item has its own link (`#item-14047`), handy for bookmarks.

## Hosting free on GitHub Pages

1. Push this folder to a new GitHub repo. Don't put "WoW", "Warcraft" or "Blizzard" in the
   repo name, since that becomes your URL (Blizzard terms).
2. Settings > Secrets and variables > Actions: add `BLIZZARD_CLIENT_ID` and `BLIZZARD_CLIENT_SECRET`.
3. Settings > Pages > Source: choose **GitHub Actions**.
4. Actions tab > "Update AH data" > Run workflow. After that it runs every hour on its own.

Price data is never committed to `main`. History lives on a `data` branch that is replaced by
one fresh commit each run, so old snapshots don't linger in git history.

## Options in config.json

- `price_field`: `"min"` (lowest listed price, matches Undermine) or `"p10"` (price at the
  cheapest 10% of supply, which ignores one-off cheap listings and gives smoother charts).

## Staying within Blizzard's API terms

What the project already does:
- **30-day limit:** price history and item names older than 30 days are deleted or refreshed
  every run, and git never keeps old copies. That's why the longest chart range is 1M.
- **Attribution:** the footer names Blizzard as the source and says the site isn't affiliated
  with or endorsed by Blizzard.
- **Privacy policy:** `privacy.html`, linked from the footer.
- **Rate limits:** a few API calls per hour after the first run, far under 36,000/hour.
- **Secrets:** your client secret lives only in GitHub Secrets and your Terminal session.

What's on you: keep it free (no ads, donation walls or paid features), keep your client
secret private, and keep Blizzard trademarks out of the site name and URL.

## Caveats

- These Forever API routes are unofficial (found by community probing at launch) and could
  change. If the fetcher starts getting 404s, that's the first thing to check.
- Prices are asking prices (what's listed), not completed sales.
- Gear with random suffixes ("of the Bear") is grouped under one item; the price is the cheapest variant.
- Bid-only auctions with no buyout are ignored.
