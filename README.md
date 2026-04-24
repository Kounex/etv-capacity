# 🏸 ETV Badminton – Hallenkapazität

Streamlit app to track how many players are currently in each badminton training hall, across every weekday.

## Prerequisites

1. **Google Cloud service account** with the Google Sheets API enabled.
2. **Google Sheet** shared with the service account's email address (editor access).

## Quick Start

```bash
# 1. Copy the env template and fill in your values
cp .env.example .env

# 2. Start the app
docker compose up -d --build

# 3. Open in browser
open http://localhost:8501
```

## Configuration

| Variable | Required | Description |
|---|---|---|
| `GOOGLE_SHEET_ID` | ✅ | The ID from your Google Sheet URL |
| `GOOGLE_SHEET_NAME` | ❌ | Worksheet tab name (default: `Kapazität`) |
| `GOOGLE_CREDENTIALS_FILE` | ✅* | Path to service-account JSON |
| `GOOGLE_CREDENTIALS_JSON` | ✅* | Service-account JSON as string |

\* Provide **one** of the two credential options.

## Local Development (without Docker)

```bash
# 1. Create a virtual environment
python3 -m venv venv

# 2. Activate it
source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Set up your environment variables
cp .env.example .env
# Edit .env and fill in GOOGLE_SHEET_ID + one credential option

# 5. Run the Streamlit app
streamlit run app.py
```

> The app will be available at **http://localhost:8501**

To deactivate the virtual environment when you're done:

```bash
deactivate
```

