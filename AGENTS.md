# ETV Hallenkapazität - Agent Guide

This document contains essential context for AI agents working in this repository.

## Repository Overview
**Project Type**: Streamlit Python Application
**Purpose**: A web UI to track badminton hall capacity for the ETV sports club, using Google Sheets as the database backend.
**Key Technologies**: Python, Streamlit, Google Sheets API (`gspread`, `google-auth`), `pandas`, `Babel`.

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
- **Data Fetching Strategy**:
  - **Initial load** (`fetch_all_initial`): Opens the spreadsheet once and reads all three tabs in a single session (4 API calls total). Kapazität is **range-limited**: only the tail rows needed to cover 2 weeks of submissions are fetched, calculated as `max(100, len(Stammdaten) × 4)`. This prevents unbounded growth of the initial load time.
  - **Submit refresh** (`fetch_recent_capacity`): On submission, only the Kapazität tab is re-fetched (2 API calls) to check for concurrent duplicates. Stammdaten and Kürzel are already cached and not re-read.
  - **Statistics history** (`fetch_full_capacity`): The complete Kapazität history is **lazy-loaded** only when the statistics tab is first opened (2 API calls). It is never fetched for users who only use the overview.
  - The Datum column is **pre-converted to ISO strings** (`_parse_date_column`) at load time in all three paths, so `find_existing_entry` can do a direct string comparison instead of a type-coercing `.astype(str)` on every call.
- **State Management & Caching**:
  - Google Client is cached via `@st.cache_resource`.
  - Sheet data is stored in `st.session_state`:
    - `_cached_config_df` / `_cached_short_names`: set on initial load, never invalidated (Stammdaten and Kürzel rarely change).
    - `_cached_data_df`: recent Kapazität rows for the overview. After a successful submit, the new row is **appended in-place** into this DataFrame — it is never set to `None` after a submit, so the overview reloads instantly without a spinner. It is only set to `None` as a fallback when a cache update cannot be performed.
    - `_cached_full_data_df`: full Kapazität history for statistics. Set to `None` after any write so the statistics tab refetches on next open.
  - Other state variables: `selected_training`, `is_submitting`, `pending_toast`, `api_error`.
  - Success messages are passed via `st.session_state.pending_toast` across reruns.
- **Google API Error Handling**:
  - `is_quota_error(exc)` detects HTTP 429 / quota / rate-limit errors from the Google API.
  - **Initial load errors** are stored in `st.session_state.api_error` and displayed as a full-page error with a "Erneut versuchen" retry button that clears the cache and reruns.
  - **Submit errors** are shown inline on the detail page (the user stays in context and can retry or go back).
  - **Statistics load errors** are shown inline in the statistics tab; `_cached_full_data_df` is left as `None` so the next tab open retries automatically.
  - **Override dialog errors** are shown inline inside the dialog.
- **Localization**:
  - All date formatting uses the `Babel` library with a global `APP_LOCALE` constant (default: `de_DE`), configurable via the `APP_LOCALE` environment variable. No hardcoded weekday/month maps exist.

## Configuration Variables
Environment variables (typically loaded via `python-dotenv` from `.env`) drive the app:
- `GOOGLE_SHEET_ID`: Target Spreadsheet ID.
- `GOOGLE_CREDENTIALS_FILE`: Path to service-account JSON, OR
- `GOOGLE_CREDENTIALS_JSON`: Service-account JSON as a raw string.
- `GOOGLE_CONFIG_SHEET_NAME`: Target config tab (defaults to "Stammdaten").
- `GOOGLE_SHEET_NAME`: Target data tab (defaults to "Kapazität").
- `ENABLE_STATISTICS_TAB`: Set to `true` to show the statistics & history tab (defaults to `false`).
- `APP_LOCALE`: Babel locale string for date formatting (defaults to `de_DE`).
- `APP_VERSION`: Used by `docker-compose.yml` to select the image tag (defaults to the version in `pyproject.toml`).

## Dependency Management
- **Single source of truth**: All dependencies are declared in `pyproject.toml`. There is no separate `requirements.txt` for production.
- **Install**: `pip install .` (both locally and in the `Dockerfile`).

## CI/CD Pipeline
- Handled by GitHub Actions (`.github/workflows/ci.yaml`).
- Triggers on pushes to the **`release`** branch.
- Reads the application version directly from `pyproject.toml` (`grep '^version = '`), builds a Docker image, and pushes it to GHCR with a version tag only (no `latest`).

## Gotchas & Important Patterns
- **Cache Update After Submit**: After a successful submit, the new entry is appended directly into `_cached_data_df` (using `pd.concat` with the row dict). Do **not** set `_cached_data_df = None` after a submit — this would trigger an unnecessary reload spinner on the overview page. Only set it to `None` as a fallback when an in-place update is not possible.
- **Concurrent Write Detection**: During form submission, `fetch_recent_capacity` fetches fresh data. If an entry for that specific slot/day was created by another user in the interim, the app prompts an override dialog (`confirm_override_dialog`) which calls `update_entry` instead of appending a duplicate row. After a confirmed override, the matching row is updated in-place in `_cached_data_df` using `data_df.loc[matching.index[-1], "Kapazität"] = new_label`.
- **`update_entry` is Range-Limited**: `update_entry` reads only column A to count rows, then scans at most the last 500 rows via a range-limited `get_values` call. Conflicts are always recent entries, so a full-sheet scan is never needed.
- **Timeline Visualization (Altair)**: The schedule uses a faceted `mark_rect` layered with `mark_text` to show time blocks precisely on a Y-axis. The data requires manual date/time interpolation (`MidH`, `StartH`, `EndH`) for rendering. Missing data generates placeholder grey blocks (`Num=0`).
- **Streamlit Re-runs (`st.rerun()`)**: When UI navigation happens or form submissions are processing, the app mutates `st.session_state` and immediately calls `st.rerun()`. Mutate state *before* calling rerun. Do **not** call `st.rerun()` before all state mutations are complete.
- **CSS Customization**: The UI styling (fonts, ETV red primary color, hiding the Streamlit sidebar, custom segmented control widths) is forced via raw HTML/CSS injection at the top of `main()`. Newly added Streamlit components should align with these overrides.
- **Streamlit Widget API**: Use `width="stretch"` instead of the deprecated `use_container_width=True` for buttons and charts.
