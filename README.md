# PC parts price tracker

Checks your Amazon.sa links once a day on GitHub's servers (your laptop can be off),
logs every price to `prices.csv`, and sends a Telegram message when:

- a part reaches your **target** price
- a part hits its **lowest logged price**
- a price **jumps or drops 10%+** since the last check
- a page **could not be read** (so failures are never silent)

It only reads public pages. It never logs in or buys anything.

## Setup (about 10 minutes, all free)

### 1. Make the Telegram bot
1. In Telegram, search for **@BotFather** and send `/newbot`. Pick a name. It gives you a **token**.
2. Open a chat with your new bot and send it any message (for example "hi").
3. Open this in a browser, with your token filled in:
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
   Find `"chat":{"id":123456789` . That number is your **chat id**.

### 2. Make the GitHub repo
1. On github.com click **New repository**. Name it `price-tracker`. Choose **Private**. Create it.
2. Click **uploading an existing file** and drag in everything from this folder.
   Make sure the `.github/workflows/price-check.yml` file keeps its folder path
   (if the web uploader drops hidden folders, use **Add file > Create new file** and type
   `.github/workflows/price-check.yml` as the name, then paste the contents).
3. Go to **Settings > Secrets and variables > Actions > New repository secret** and add two secrets:
   - `TELEGRAM_TOKEN` = your bot token
   - `TELEGRAM_CHAT_ID` = your chat id

### 3. Test it
Open the **Actions** tab, choose **Daily price check**, press **Run workflow**.
Within a couple of minutes you should get a Telegram message (alerts or a "could not read" list),
and `prices.csv` appears in the repo.

After that it runs by itself every day at 09:00 Saudi time.

## Editing your parts
Edit `parts.json` on GitHub (pencil icon). Each part has:
- `name`, `url`
- `last_seen`: the price you last saw (for your own reference)
- `target`: alert when the price is at or below this (SAR)

## If Amazon blocks it
GitHub's servers are cloud addresses and Amazon may show a captcha. If most or all pages fail:
- You will get a message saying so. Prices will be blank in `prices.csv` for those days.
- Fallbacks: check Keepa/CamelCamelCamel to see whether they cover Amazon.sa, or type prices
  into `prices.csv` by hand each week (same columns: date,name,price,status,url, with status `ok`).

## Notes
- Scheduled runs can start 10-30 minutes late.
- GitHub can pause scheduled workflows after about 60 days without repo activity. If it stops,
  open the Actions tab and re-enable it.
- The "best time to buy" signal is simply: at or below your target, or the lowest price in the log.
  It cannot predict future prices.
