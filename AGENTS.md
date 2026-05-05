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
  pip install .
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
  - **Kürzel**: Provides a mapping of long hall names to their short abbreviations (e.g. 'ALTO', 'GH') for UI styling.
  - **Kapazität (Data Sheet)**: The live database where capacity ratings ("Noch gut Luft", "Passt", "Zu voll") are appended or updated.
- **State Management & Data Fetching**:
  - Google Client is cached via `@st.cache_resource`.
  - The Config Sheet is cached via `@st.cache_data` (TTL 300s).
  - The Data Sheet (`fetch_data`) is fetched on demand without automatic `@st.cache_data` to ensure live readings, but sometimes temporarily cached in `st.session_state._cached_data_df` to prevent redundant fetches during a single UI flow.
  - State variables (like `st.session_state.selected_training`, `st.session_state.edit_mode`, `st.session_state.is_submitting`) manage UI navigation and processing states.
  - Success messages are passed via `st.session_state.pending_toast` across reruns.

## Configuration Variables
Environment variables (typically loaded via `python-dotenv` from `.env`) drive the app:
- `GOOGLE_SHEET_ID`: Target Spreadsheet ID.
- `GOOGLE_CREDENTIALS_FILE`: Path to service-account JSON, OR
- `GOOGLE_CREDENTIALS_JSON`: Service-account JSON as a raw string.
- `GOOGLE_CONFIG_SHEET_NAME`: Target config tab (defaults to "Stammdaten").
- `GOOGLE_SHEET_NAME`: Target data tab (defaults to "Kapazität").

## CI/CD Pipeline
- Handled by GitHub Actions (`.github/workflows/ci.yaml`).
- Triggers on pushes to the **`release`** branch.
- Reads the application version directly from `pyproject.toml` (`grep '^version = '`), builds a Docker image, and pushes it to GHCR.

## Gotchas & Important Patterns
- **Timeline Visualization (Altair)**: The schedule uses a faceted `mark_rect` layered with `mark_text` to show time blocks precisely on a Y-axis. The data requires manual date/time interpolation (`MidH`, `StartH`, `EndH`) for rendering. Missing data generates placeholder grey blocks (`Num=0`).
- **Streamlit Re-runs (`st.rerun()`)**: When UI navigation happens or form submissions are processing, the app mutates `st.session_state` and immediately calls `st.rerun()`. Always invalidate caches (like `st.session_state._cached_data_df = None`) *before* calling rerun to ensure fresh data.
- **Concurrency Prevention**: During form submission, the app performs a fresh data fetch (`fetch_data`). If an entry for that specific class/day was created by another user in the interim, it prompts an override dialog (`confirm_override_dialog`) which calls an update function (`update_entry`) instead of appending a duplicate row.
- **CSS Customization**: The UI styling (fonts, ETV red primary color, hiding the Streamlit sidebar, custom segmented control widths) is forced via raw HTML/CSS injection at the top of `main()`. Newly added Streamlit components should align with these overrides.
- **German Localization**: Hardcoded German weekday strings (`Montag`...) and date parsing/logic are used to identify "Today" and calculate "Missing Entries" from the past two weeks.
