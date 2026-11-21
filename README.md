# plugin-poopjob

A Stavrobot plugin for [PoopJob](https://poopjob.app/), the baby-tracking app.
It lets the assistant log and look up diapers, breastfeeding sessions, bottle
feedings, spit-ups and weights, and time a breastfeeding session as it happens.

## Before you install

You need a PoopJob account and an API key:

1. Open the PoopJob app and go to **Settings**.
2. Under **API keys**, choose **Create API key** and give it a name.
3. Copy the key. It starts with `pj_` and is shown only once.

The key has full access to your PoopJob log, so treat it like a password. You
can revoke it in the same place at any time.

## Installation

Tell Stavrobot to install `https://github.com/stavrobot/plugin-poopjob.git`.

## Configuration

- `api_key`: your PoopJob API key (see above).
- `timezone`: your timezone as an IANA name, for example `Europe/Athens` or
  `America/New_York`. The plugin uses it to read times like "at 3pm" and to
  show logged times in your local time.

## Tools

- **log_diaper**: log a poop, pee or mixed diaper, with an optional stool color.
- **log_bottle**: log a bottle feeding, in milliliters, of breast milk or formula.
- **log_spit_up**: log a small, medium or large spit-up.
- **log_weight**: log the baby's weight, in grams.
- **log_breastfeeding**: log a breastfeeding session that already happened, side by side.
- **breastfeeding_timer**: start, switch sides, pause, resume, end or cancel a live
  breastfeeding session, or check the one in progress.
- **list_entries**: list recent entries of one kind or of every kind, optionally
  within a time range.
- **update_entry**: change a logged entry, for example to fix its time.
- **delete_entry**: delete a logged entry.

Times default to now when you do not give one. The plugin never retries a failed
log, so an entry is not logged twice by accident.
