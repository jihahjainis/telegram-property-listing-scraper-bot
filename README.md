# Telegram Property Scraper Bot

Telegram tools for finding rental listing messages, forwarding matches into a target Telegram group, and exporting extracted listing details to Excel.

## Features

- Runs as a Telegram bot with the `/rent` command.
- Searches configured Telegram folders or fallback groups.
- Filters messages by include and exclude keywords.
- Skips unavailable listings based on status keywords.
- Forwards matching listings to a target group.
- Extracts listing details with Anthropic and writes them to `listings.xlsx`.

## Requirements

- Python 3.14 or compatible Python 3 version
- Telegram API ID and hash
- Telegram bot token from BotFather
- Anthropic API key

Install dependencies:

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Configuration

Create a local `.env` file from the example:

```bash
cp .env.example .env
```

Then fill in:

```bash
TELEGRAM_API_ID=your_api_id
TELEGRAM_API_HASH=your_api_hash
TELEGRAM_SESSION=/path/to/property_session
TELEGRAM_BOT_TOKEN=your_bot_token_from_botfather
TELEGRAM_ALLOWED_USERS=your_telegram_user_id
ANTHROPIC_API_KEY=your_anthropic_api_key
```

Do not commit `.env` or Telegram `.session` files. They contain credentials and account session data.

## Usage

Run the Telegram bot:

```bash
python bot.py
```

In Telegram, send `/rent` to the bot from an allowed user account.

Run the interactive scraper directly:

```bash
python searchListRent.py
```

Run the monthly variant:

```bash
python searchListRentMonth.py
```

## Generated Files

The scraper writes listing output to `listings.xlsx`. This file is ignored by Git.
