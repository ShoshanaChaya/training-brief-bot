# Training Brief Bot

A daily training briefing for endurance athletes. Every morning it pulls the previous day's sleep, HRV and activity data from Garmin Connect, asks Gemini to write a workout and nutrition plan against the athlete's profile and race goal, and emails the result before the day starts.

```
GitHub Actions cron (06:00)
→ fetch yesterday's sleep, HRV and activities from Garmin
→ ask Gemini (2.5 Flash) to write today's plan
→ send it by email via Resend
→ read it on your phone
```

Runs entirely on free tiers — no server, no database. GitHub Actions provides the scheduler, and state that must survive between runs is committed back to the repository.

## Structure

| Module | Responsibility |
|---|---|
| `src/garmin_client.py` | Authenticates against Garmin Connect and pulls sleep, HRV and activity data |
| `src/coach.py` | Builds the prompt from the athlete profile and yesterday's metrics, calls Gemini |
| `src/emailer.py` | Renders the briefing and sends it via Resend |
| `src/main.py` | Orchestrates the daily run |
| `prompts/coach_system.md` | System prompt defining how the coach reasons about training load |
| `baseline.example.yaml` | Athlete profile template — copy to `baseline.yaml` and fill in |

## Setup

**Credentials.** Copy `.env.example` to `.env` and fill in your Gemini API key, Garmin Connect credentials and Resend API key. For scheduled runs, set each as a repository secret instead — the workflow reads them from there.

**Athlete profile.** Copy `baseline.example.yaml` to `baseline.yaml` and fill in the profile and goal race. That file is gitignored; it holds personal data and stays local.

**Dependencies.** `pip install -r requirements.txt`

**Run it.** `python -m src.main` sends one briefing. The scheduled workflow in `.github/workflows/daily.yml` does it every morning.

## Notes

Garmin has no official public API, so this uses the `python-garminconnect` library against the same endpoints the mobile app uses. It works, but it's unofficial and can break when Garmin changes things.

Gemini 2.5 Flash was chosen for the free tier — roughly 1500 requests a day, far more than one briefing needs.
