# ETV Hallenkapazität - Agent Guide

This document contains essential context for AI agents working in this repository.

## Repository Overview
**Project Type**: Streamlit Python Application
**Purpose**: A web UI to track badminton hall capacity for the ETV sports club, using Google Sheets as the database backend.
**Key Technologies**: Python, Streamlit, Google Sheets API (`gspread`, `google-auth`), `pandas`.

## Essential Commands
- **Local Development**:
  ```bash
  python3 -m venv venv
  source venv/bin/activate
  pip install -r requirements.txt
  streamlit run app.py
  ```
- **Docker Build/Run**:
  ```bash
  docker compose up -d --build
  ```

## Architecture & Data Flow
- **Single-File Structure**: All UI rendering, state logic, and database operations are contained in `app.py`.
- **Database (Google Sheets)**:
  - **Stammdaten (Config Sheet)**: Defines the master schedule (Day, Time, Training Type, Hall). The app reads this to know what classes exist.
  - **Kapazität (Data Sheet)**: The live database where capacity ratings ("Noch gut Luft", "Passt", "Zu voll") are appended or updated.
- **Caching & State Management**:
  - Google Client is cached via `@st.cache_resource`.
  - The Config Sheet is cached via `@st.cache_data` (TTL 300s).
  - The Data Sheet overview uses `@st.fragment(run_every=10)` coupled with a `@st.cache_data(ttl=10)` function (`fetch_data_cached`). This auto-refreshes the list every 10 seconds in the background without a full page reload, conserving Google API limits.
  - Manual state variables (`st.session_state.selected_training`) are used for navigation. Cache invalidation (`fetch_data_cached.clear()`) is performed after successful submission to ensure immediate UI freshness upon return to the overview.

## Configuration Variables
Environment variables (typically loaded via `python-dotenv` from `.env`) drive the app:
- `GOOGLE_SHEET_ID`: Target Spreadsheet ID.
- `GOOGLE_CREDENTIALS_FILE`: Path to service-account JSON, OR
- `GOOGLE_CREDENTIALS_JSON`: Service-account JSON as a raw string.

## CI/CD Pipeline
- Handled by GitHub Actions (`.github/workflows/ci.yaml`).
- Triggers on pushes to the **`release`** branch.
- Reads the repository's `VERSION` file, builds a Docker image, and pushes it to GHCR.

## Gotchas & Important Patterns
- **Streamlit Re-runs (`st.rerun()`)**: When UI navigation happens (like selecting a class to review), the app mutates `st.session_state` (`selected_training`, `_data_stale`) and immediately calls `st.rerun()`. Avoid modifying state blindly without handling the rerun cycle.
- **Concurrency Prevention**: During form submission, the app performs a fresh data fetch. If an entry for that specific class/day was created by another user in the interim, it performs a row update (`update_entry`) instead of appending a duplicate row.
- **CSS Customization**: The UI styling (fonts, ETV red primary color, hiding the Streamlit sidebar) is forced via raw HTML/CSS injection at the top of `main()`. Newly added Streamlit components should align with these overrides.
- **German Localization**: Hardcoded German weekday strings (`Montag`...) and date parsing/logic are used to identify "Today" and calculate "Missing Entries" from the past two weeks.
