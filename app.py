"""
ETV Hallenkapazität - Streamlit Application

This application allows users to track and report the capacity of badminton halls
for the ETV sports club. It uses Google Sheets as a database backend to store
configuration (Stammdaten) and live capacity data.

Key architectural decisions:
- Stammdaten, Kürzel, and a range-limited tail of Kapazität are all fetched
  in a single spreadsheet open via ``fetch_all_initial`` on page load.
- Kapazität data is fetched with a range-limited read via ``fetch_recent_capacity``,
  sized to cover 2 weeks of possible submissions (with a 4× re-submission buffer)
  based on the Stammdaten count. This is also used at submit time to detect
  concurrent writes and trigger the override-confirmation dialog.
- Full Kapazität history is lazy-loaded only when the statistics tab is accessed
  via ``fetch_full_capacity``.
- The Kapazität sheet is **append-only**: every confirmed submission (including
  "overrides") appends a new row. The latest row per (Datum, slot) is the current
  value. Consumers use ``find_existing_entry`` (``iloc[-1]``) for the overview and
  ``drop_duplicates(keep="last")`` for statistics.
- Each row now carries an ``Eingetragen am`` column with the wall-clock submission
  timestamp (ISO 8601 local, ``YYYY-MM-DDTHH:MM:SS``), distinct from ``Datum``
  which is the training date the entry refers to.
- Sheet data is cached in ``st.session_state`` so that UI interactions
  (tab switches, button clicks) operate from memory without re-fetching.
- The Datum column is pre-converted to ISO strings at load time for fast lookups.
- Date formatting uses the Babel library with a configurable ``APP_LOCALE``
  constant (default: de_DE) for easy multi-language support.
"""

import json
import os
from collections import defaultdict
from datetime import date, datetime, timedelta

import gspread
from gspread.exceptions import APIError
import pandas as pd
import streamlit as st
import altair as alt
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
import babel.dates

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Google Sheet tab names
CONFIG_SHEET_NAME = os.getenv("GOOGLE_CONFIG_SHEET_NAME", "Stammdaten")
DATA_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "Kapazität")

# Feature Flags
ENABLE_STATISTICS_TAB = os.getenv("ENABLE_STATISTICS_TAB", "false").lower() == "true"

# Localization
APP_LOCALE = os.getenv("APP_LOCALE", "de_DE")


# ---------------------------------------------------------------------------
# Google API error handling
# ---------------------------------------------------------------------------

def is_quota_error(exc: Exception) -> bool:
    """Check whether an exception is a Google API quota / rate-limit error."""
    if isinstance(exc, APIError):
        response = getattr(exc, "response", None)
        if response is not None and getattr(response, "status_code", None) == 429:
            return True
        msg = str(exc).lower()
        if "429" in msg or "quota" in msg or "rate limit" in msg or "too many requests" in msg:
            return True
    msg = str(exc).lower()
    if "429" in msg or "quota" in msg or "rate limit" in msg or "too many requests" in msg:
        return True
    return False


# Traffic-light capacity options
CAPACITY_OPTIONS = {
    "🟢 Noch gut Luft": "Noch gut Luft",
    "🟡 Passt": "Passt",
    "🔴 Zu voll": "Zu voll",
}

CONFIG_HEADERS = [
    "Wochentag",
    "Uhrzeit",
    "Trainingsart",
    "Halle",
    "Max. Kapazität",
]

DATA_HEADERS = [
    "Datum",
    "Wochentag",
    "Uhrzeit",
    "Trainingsart",
    "Halle",
    "Kapazität",
    "Eingetragen am",
]

# ---------------------------------------------------------------------------
# Google Sheets helpers
# ---------------------------------------------------------------------------


@st.cache_resource(show_spinner="Verbindung wird hergestellt...")
def get_gspread_client() -> gspread.Client:
    """Authenticate with Google and return a gspread client.

    Credentials are read from either:
      • a JSON file whose path is in the GOOGLE_CREDENTIALS_FILE env var, or
      • a JSON string stored in the GOOGLE_CREDENTIALS_JSON env var
        (handy for container / secret-manager deployments).
    """
    creds_file = os.getenv("GOOGLE_CREDENTIALS_FILE")
    creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")

    if creds_file:
        creds = Credentials.from_service_account_file(creds_file, scopes=SCOPES)
    elif creds_json:
        info = json.loads(creds_json)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        st.error(
            "Keine Google-Credentials gefunden. Bitte setze "
            "`GOOGLE_CREDENTIALS_FILE` oder `GOOGLE_CREDENTIALS_JSON`."
        )
        st.stop()

    return gspread.authorize(creds)


def get_or_create_worksheet(
    client: gspread.Client,
    spreadsheet_id: str,
    sheet_name: str,
    headers: list[str] | None = None,
) -> gspread.Worksheet:
    """Return the worksheet *sheet_name*, creating it if necessary."""
    spreadsheet = client.open_by_key(spreadsheet_id)
    try:
        return spreadsheet.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=sheet_name, rows=1000, cols=10)
        if headers:
            ws.append_row(headers)
        return ws


def _records_from_values(values: list[list[str]]) -> list[dict[str, str]]:
    """Convert a ``get_values`` result (including header row) to record dicts."""
    if len(values) <= 1:
        return []
    headers = values[0]
    return [dict(zip(headers, row)) for row in values[1:]]


def fetch_all_initial(
    spreadsheet_id: str,
) -> tuple[pd.DataFrame, dict[str, str], pd.DataFrame]:
    """Open the spreadsheet **once** and read all 3 tabs for initial page load.

    Kapazität is read with a range-limited tail based on the Stammdaten count.
    Makes exactly 4 API calls (same as the old ``fetch_all_sheets``).
    """
    client = get_gspread_client()
    spreadsheet = client.open_by_key(spreadsheet_id)

    # --- Stammdaten (config) ---
    try:
        config_ws = spreadsheet.worksheet(CONFIG_SHEET_NAME)
    except gspread.exceptions.WorksheetNotFound:
        config_ws = spreadsheet.add_worksheet(
            title=CONFIG_SHEET_NAME, rows=1000, cols=10,
        )
        config_ws.append_row(CONFIG_HEADERS)
    config_records = config_ws.get_all_records()
    config_df = (
        pd.DataFrame(config_records)
        if config_records
        else pd.DataFrame(columns=CONFIG_HEADERS)
    )

    # --- Kürzel (short names) ---
    try:
        kurzel_ws = spreadsheet.worksheet("Kürzel")
        kurzel_records = kurzel_ws.get_all_records()
        short_names = {
            r["Halle"]: r["Abkürzung"]
            for r in kurzel_records
            if "Halle" in r and "Abkürzung" in r
        }
    except Exception:
        short_names = {}

    # --- Kapazität (range-limited) ---
    data_ws = spreadsheet.worksheet(DATA_SHEET_NAME)
    all_values = data_ws.get_all_values()
    total_rows = len(all_values)

    if total_rows <= 1:
        data_df = pd.DataFrame(columns=DATA_HEADERS)
    else:
        entries_per_week = len(config_df) if not config_df.empty else 20
        rows_needed = max(150, entries_per_week * 2 * 4)

        if total_rows - 1 <= rows_needed:
            data_records = _records_from_values(all_values)
        else:
            start_row = total_rows - rows_needed
            tail_values = data_ws.get_values(f"A{start_row}:H{total_rows}")
            data_records = _records_from_values(tail_values)

        data_df = pd.DataFrame(data_records) if data_records else pd.DataFrame(columns=DATA_HEADERS)

    return config_df, short_names, _parse_date_column(data_df)


def fetch_recent_capacity(
    spreadsheet_id: str,
    config_df: pd.DataFrame,
) -> pd.DataFrame:
    """Fetch Kapazität rows for the current overview.

    Row count is based on Stammdaten: covers 2 weeks of submissions
    with a 2x buffer for duplicates/overrides (minimum 100 rows).
    """
    data_ws = _get_data_worksheet(spreadsheet_id)
    all_values = data_ws.get_all_values()
    total_rows = len(all_values)

    if total_rows <= 1:
        return pd.DataFrame(columns=DATA_HEADERS)

    entries_per_week = len(config_df) if not config_df.empty else 20
    rows_needed = max(150, entries_per_week * 2 * 4)

    if total_rows - 1 <= rows_needed:
        data_records = _records_from_values(all_values)
    else:
        start_row = total_rows - rows_needed
        tail_values = data_ws.get_values(f"A{start_row}:H{total_rows}")
        data_records = _records_from_values(tail_values)

    data_df = pd.DataFrame(data_records) if data_records else pd.DataFrame(columns=DATA_HEADERS)
    return _parse_date_column(data_df)


def _parse_date_column(df: pd.DataFrame, column: str = "Datum") -> pd.DataFrame:
    """Pre-convert a date column to ISO-format strings for fast comparison."""
    if df.empty or column not in df.columns:
        return df
    try:
        df[column] = pd.to_datetime(df[column], format="mixed").dt.strftime("%Y-%m-%d")
    except Exception:
        df[column] = df[column].astype(str)
    return df


def _get_data_worksheet(spreadsheet_id: str) -> gspread.Worksheet:
    """Return the Kapazität worksheet, creating it if it does not exist."""
    client = get_gspread_client()
    spreadsheet = client.open_by_key(spreadsheet_id)
    try:
        return spreadsheet.worksheet(DATA_SHEET_NAME)
    except gspread.exceptions.WorksheetNotFound:
        data_ws = spreadsheet.add_worksheet(
            title=DATA_SHEET_NAME, rows=1000, cols=10,
        )
        data_ws.append_row(DATA_HEADERS)
        return data_ws


def fetch_full_capacity(spreadsheet_id: str) -> pd.DataFrame:
    """Fetch complete Kapazität history for the statistics tab."""
    data_ws = _get_data_worksheet(spreadsheet_id)
    data_records = data_ws.get_all_records()
    data_df = (
        pd.DataFrame(data_records)
        if data_records
        else pd.DataFrame(columns=DATA_HEADERS)
    )
    return _parse_date_column(data_df)


def find_existing_entry(
    data_df: pd.DataFrame,
    today_iso: str,
    weekday: str,
    time_slot: str,
    training_type: str,
    hall: str,
) -> pd.Series | None:
    """Return the row for today's entry matching the training slot, or None.

    The Datum column is expected to be pre-converted to ISO strings
    (handled by ``_parse_date_column`` during data load).
    """
    mask = (
        (data_df["Datum"] == today_iso)
        & (data_df["Wochentag"] == weekday)
        & (data_df["Uhrzeit"] == time_slot)
        & (data_df["Trainingsart"] == training_type)
        & (data_df["Halle"] == hall)
    )
    matches = data_df[mask]
    if matches.empty:
        return None
    return matches.iloc[-1]


def submit_entry(
    worksheet: gspread.Worksheet,
    weekday: str,
    time_slot: str,
    training_type: str,
    hall: str,
    capacity_label: str,
    date_iso: str | None = None,
    submitted_at: str | None = None,
) -> str:
    """Append a new capacity entry and return the submission timestamp used.

    Returns the ``submitted_at`` ISO string so callers can mirror it in the
    local cache without recomputing a different timestamp.
    """
    entry_date = date_iso or date.today().isoformat()
    ts = submitted_at or datetime.now().replace(microsecond=0).isoformat()
    worksheet.append_row(
        [entry_date, weekday, time_slot, training_type, hall, capacity_label, ts]
    )
    return ts


# Reverse lookup: capacity label → emoji key
CAPACITY_LABEL_TO_KEY = {v: k for k, v in CAPACITY_OPTIONS.items()}

# Numeric mapping for statistics
CAPACITY_TO_NUM = {
    "Noch gut Luft": 1,
    "Passt": 2,
    "Zu voll": 3
}



# ---------------------------------------------------------------------------
# Streamlit UI
# ---------------------------------------------------------------------------


@st.dialog("Bestätigung: Eintrag überschreiben")
def confirm_override_dialog(
    existing_label: str, new_label: str, spreadsheet_id: str, entry_date: str, t: dict
) -> None:
    """Show a dialog to confirm overwriting an existing capacity entry.

    Args:
        existing_label: The capacity label currently stored.
        new_label: The new capacity label chosen by the user.
        spreadsheet_id: The Google Sheets ID.
        entry_date: The date of the entry in ISO format.
        t: Dictionary containing the training details.
    """
    st.warning(f"Für diese Halle wurde bereits **{existing_label}** eingetragen.")
    st.markdown(f"Möchtest du dies wirklich mit **{new_label}** überschreiben?")

    col1, col2 = st.columns(2)
    if col1.button("Abbrechen", width="stretch"):
        st.rerun()
    if col2.button("Überschreiben", type="primary", width="stretch"):
        try:
            client = get_gspread_client()
            data_ws = get_or_create_worksheet(
                client, spreadsheet_id, DATA_SHEET_NAME, headers=DATA_HEADERS
            )
            submitted_at = submit_entry(
                data_ws,
                t["Wochentag"],
                t["Uhrzeit"],
                t["Trainingsart"],
                t["Halle"],
                new_label,
                date_iso=entry_date,
            )
            new_label_with_emoji = CAPACITY_LABEL_TO_KEY.get(new_label, new_label)
            st.session_state.pending_toast = {
                "msg": f"**{new_label_with_emoji}** für **{t['Trainingsart']}** ({t['Uhrzeit']}, {t['Halle']}) eingetragen!",
            }
            # Append the new row into the cache so the overview reflects the
            # latest value instantly. find_existing_entry uses iloc[-1], so
            # appending here is enough — no in-place mutation needed.
            data_df = st.session_state._cached_data_df
            if data_df is not None:
                new_row = pd.DataFrame([{
                    "Datum": entry_date,
                    "Wochentag": t["Wochentag"],
                    "Uhrzeit": t["Uhrzeit"],
                    "Trainingsart": t["Trainingsart"],
                    "Halle": t["Halle"],
                    "Kapazität": new_label,
                    "Eingetragen am": submitted_at,
                }])
                st.session_state._cached_data_df = pd.concat(
                    [data_df, new_row], ignore_index=True
                )
            else:
                st.session_state._cached_data_df = None  # fallback: force reload
            st.session_state._cached_full_data_df = None
            st.session_state.selected_training = None
            st.rerun()
        except Exception as exc:
            if is_quota_error(exc):
                st.error(
                    "**Google API-Limit erreicht** — Überschreiben nicht möglich.\n\n"
                    "Bitte warte ein paar Sekunden und versuche es erneut."
                )
            else:
                st.error(f"Fehler beim Überschreiben: {exc}")


def main() -> None:
    """Main Streamlit application entry point.

    Handles page configuration, custom CSS styling, session state initialization,
    and rendering of the UI components (overview list and detailed rating view).
    """
    st.set_page_config(
        page_title="ETV Hallenkapazität",
        page_icon="🏸",
        layout="centered",
    )

    # --- ETV Theme CSS ---
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Titillium+Web:wght@400;600;700;900&display=swap');

        /* Global font */
        html, body, [class*="css"] {
            font-family: "Titillium Web", sans-serif !important;
        }

        /* Headlines – ETV style */
        h1, h2, h3 {
            font-family: "Titillium Web", sans-serif !important;
            text-transform: uppercase !important;
            letter-spacing: 1px !important;
            font-weight: 900 !important;
        }
        h1 {
            color: #DC0D15 !important;
            font-size: 2rem !important;
            line-height: 1.2 !important;
        }
        h2 {
            color: #DC0D15 !important;
            font-size: 1.3rem !important;
        }

        /* Primary buttons – ETV red */
        button[kind="primary"]:not(:disabled) {
            background-color: #DC0D15 !important;
            border-color: #DC0D15 !important;
            color: white !important;
        }
        button[kind="primary"]:not(:disabled):hover {
            background-color: #b00a11 !important;
            border-color: #b00a11 !important;
        }

        /* Force segmented control to full width */
        div[data-testid="stElementContainer"]:has([data-testid="stButtonGroup"]) {
            width: 100% !important;
            max-width: none !important;
        }
        div[data-testid="stButtonGroup"] {
            width: 100% !important;
            max-width: none !important;
            margin-top: 0.5rem;
            margin-bottom: 1rem;
        }
         [data-baseweb="button-group"][role="radiogroup"] {
            width: 100% !important;
            max-width: none !important;
            display: flex !important;
            flex-wrap: nowrap !important;
            flex-direction: column;
            gap: 12px;
        }

        [data-baseweb="button-group"][role="radiogroup"] > button {
            border-radius: 0.5rem !important;
            border: 1px solid rgba(26, 26, 26, 0.2) !important;
        }

        /* Card containers */
        div[data-testid="stVerticalBlock"] > div[data-testid="stExpander"] {
            border-color: #eee !important;
        }

        /* ETV logo header */
        .etv-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 1rem;
            padding-bottom: 1rem;
            margin-bottom: 1.5rem;
            border-bottom: 2px solid #eee;
        }
        .etv-header svg {
            height: 50px;
            width: auto;
            fill: #DC0D15;
        }
        .etv-header .etv-title {
            font-family: "Titillium Web", sans-serif;
            font-weight: 900;
            font-size: 1.8rem;
            color: #DC0D15;
            text-transform: uppercase;
            letter-spacing: 1px;
            line-height: 1.2;
        }
        .etv-header .etv-subtitle {
            font-family: "Titillium Web", sans-serif;
            font-weight: 400;
            font-size: 0.95rem;
            color: #666;
            text-transform: uppercase;
            letter-spacing: 1px;
        }
        .etv-day-tag {
            font-family: "Titillium Web", sans-serif;
            font-size: 0.9rem;
            color: #333;
            border: 1.5px solid #ddd;
            border-radius: 6px;
            padding: 0.4rem 0.9rem;
            white-space: nowrap;
        }
        .etv-day-tag strong {
            color: #DC0D15;
        }
        @media (max-width: 500px) {
            .etv-header {
                flex-direction: column;
                align-items: flex-start;
                gap: 0.5rem;
            }
            /* Clamp Vega-Lite tooltip inside the viewport on mobile */
            #vg-tooltip-element {
                max-width: 75vw !important;
                left: 12px !important;
                right: auto !important;
                font-size: 0.85rem !important;
                box-sizing: border-box !important;
            }
            
            /* Make the heatmap scrollable on mobile instead of shrinking */
            [data-testid="stVegaLiteChart"] {
                width: 100% !important;
                overflow-x: auto !important;
                overflow-y: hidden !important;
                -webkit-overflow-scrolling: touch;
            }
            [data-testid="stVegaLiteChart"] canvas,
            [data-testid="stVegaLiteChart"] svg {
                min-width: 700px !important;
            }
        }

        /* Hide sidebar */
        [data-testid="stSidebar"] { display: none; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    def clear_selection():
        st.session_state.selected_training = None

    # --- Session state defaults ---
    if "selected_training" not in st.session_state:
        st.session_state.selected_training = None
    if "_cached_config_df" not in st.session_state:
        st.session_state._cached_config_df = None
    if "_cached_short_names" not in st.session_state:
        st.session_state._cached_short_names = None
    if "_cached_data_df" not in st.session_state:
        st.session_state._cached_data_df = None
    if "_cached_full_data_df" not in st.session_state:
        st.session_state._cached_full_data_df = None
    if "is_submitting" not in st.session_state:
        st.session_state.is_submitting = False
    if "pending_toast" not in st.session_state:
        st.session_state.pending_toast = None
    if "api_error" not in st.session_state:
        st.session_state.api_error = None

    if st.session_state.pending_toast:
        st.toast(st.session_state.pending_toast["msg"])
        st.session_state.pending_toast = None

    # --- Determine today (needed for header) ---
    today = date.today()
    today_iso = today.isoformat()
    today_weekday = babel.dates.format_date(today, "EEEE", locale=APP_LOCALE)

    # --- ETV Logo + Title + Day Tag ---
    st.markdown(
        f"""
        <div class="etv-header">
            <div style="display: flex; align-items: center; gap: 1rem;">
                <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 503.1 708.6">
                    <path d="M503.1,354.3C503.1,158.6,390.5,0,251.6,0S0,158.6,0,354.3s112.6,354.3,251.6,354.3S503.1,550,503.1,354.3
                    L503.1,354.3z M66.5,253.9H181v47h-75.3c-2.3,17.3-3.5,35.1-3.5,53.3c0,0.5,0,1.1,0,1.6h75.6v47h-72.7
                    c6.2,52.1,22.3,99.7,46.9,137.3c28.1,42.9,63.4,66.4,99.5,66.4c7.9,0,15.8-1.1,23.5-3.4v48.2c-7.7,1.4-15.6,2.1-23.5,2.1
                    c-52.4,0-101.7-31.1-138.8-87.7c-37.1-56.5-57.5-131.7-57.5-211.7C55.3,319.5,59.2,285.7,66.5,253.9L66.5,253.9z M285.8,79.4
                    c0,19-15.3,34.2-34.3,34.2c-19,0-34.2-15.3-34.2-34.2c0-19.1,15.3-34.3,34.4-34.3C270.6,45.1,285.8,60.4,285.8,79.4L285.8,79.4z
                    M422.1,206.9H275.1v349c-7.7,2.4-15.6,3.7-23.7,3.7c-8,0-15.8-1.2-23.3-3.6V206.9H81c6.3-16.6,13.6-32.3,22-47h297.2
                    C408.5,174.6,415.9,190.3,422.1,206.9L422.1,206.9z M436.6,253.9c7.4,31.8,11.3,65.6,11.3,100.3c0,80-20.4,155.1-57.5,211.7
                    c-19.7,30.1-42.9,53-68.2,67.8V253.9h47v253.5c20.6-44,31.7-97,31.7-153.2c0-35.1-4.4-69.1-12.6-100.3H436.6L436.6,253.9z"/>
                </svg>
                <div>
                    <div class="etv-title">Hallenkapazität</div>
                    <div class="etv-subtitle">Badminton</div>
                </div>
            </div>
            <div class="etv-day-tag">Heute ist <strong>{today_weekday}</strong>, der {babel.dates.format_date(today, "d. MMMM", locale=APP_LOCALE)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    spreadsheet_id = os.getenv("GOOGLE_SHEET_ID", "")

    def select_training(entry):
        st.session_state.selected_training = entry

    if not spreadsheet_id:
        st.warning(
            "Bitte setze die Umgebungsvariable `GOOGLE_SHEET_ID` "
            "mit der ID deines Google Sheets."
        )
        st.stop()

    # --- Load all data in one spreadsheet open (initial load only) ---
    if st.session_state._cached_data_df is None:
        with st.spinner("Trainingsdaten werden geladen..."):
            try:
                config_df, short_names, data_df = fetch_all_initial(spreadsheet_id)
                st.session_state._cached_config_df = config_df
                st.session_state._cached_short_names = short_names
                st.session_state._cached_data_df = data_df
                st.session_state.api_error = None
            except Exception as exc:
                if is_quota_error(exc):
                    st.session_state.api_error = (
                        "**Google API-Limit erreicht** — Die Trainingsdaten konnten nicht geladen werden.\n\n"
                        f"Bitte warte ein paar Sekunden und lade die Seite neu. "
                        "Das Limit wird von Google pro Minute gezählt."
                    )
                else:
                    st.session_state.api_error = (
                        f"**Verbindungsfehler** — Die Trainingsdaten konnten nicht geladen werden.\n\n"
                        f"Details: {exc}"
                    )

    config_df = st.session_state._cached_config_df
    short_names = st.session_state._cached_short_names

    if st.session_state.api_error:
        st.error(st.session_state.api_error)
        if st.button("Erneut versuchen", type="primary", width="stretch"):
            st.session_state.api_error = None
            st.session_state._cached_data_df = None
            st.rerun()
        st.stop()

    if config_df.empty:
        st.warning(
            f"Das Blatt **{CONFIG_SHEET_NAME}** ist leer. "
            "Bitte fülle es mit den Trainingsdaten "
            "(Wochentag, Uhrzeit, Trainingsart, Halle, Max. Kapazität)."
        )
        st.stop()

    # =====================================================================
    # STEP 1 – Training selection (landing page)
    # =====================================================================
    if st.session_state.selected_training is None:

        def render_overview():
            data_df = st.session_state._cached_data_df

            day_df = config_df[config_df["Wochentag"] == today_weekday]

            st.markdown(
                "Ihr helft uns dabei, die Auslastung unserer Hallen zu bewerten. Gebt einfach an wie voll ihr die Halle empfindet, wenn ihr da seid."
            )

            if day_df.empty:
                st.info(f"Am {today_weekday} gibt es keine Trainings.")
            else:
                # Split trainings into open / already submitted
                open_trainings = []
                submitted_trainings = []

                for idx, row in day_df.iterrows():
                    existing = find_existing_entry(
                        data_df,
                        today_iso,
                        row["Wochentag"],
                        row["Uhrzeit"],
                        row["Trainingsart"],
                        row["Halle"],
                    )
                    if existing is not None:
                        submitted_trainings.append((idx, row, existing))
                    else:
                        open_trainings.append((idx, row))

                # --- Open trainings ---
                if open_trainings:
                    st.subheader("📋 Noch offen")
                    for idx, row in open_trainings:
                        with st.container(border=True):
                            col_time, col_name, col_hall, col_btn = st.columns(
                                [2, 3, 3, 2]
                            )
                            col_time.markdown(f"🕐 {row['Uhrzeit']}")
                            col_name.markdown(f"**{row['Trainingsart']}**")
                            col_hall.markdown(f"📍 {row['Halle']}")
                            col_btn.button(
                                "Auswählen",
                                key=f"open_{idx}",
                                type="primary",
                                width="stretch",
                                on_click=select_training,
                                args=(row.to_dict(),),
                            )

                # --- Already submitted ---
                if submitted_trainings:
                    st.subheader("✅ Bereits erfasst")
                    for idx, row, existing in submitted_trainings:
                        cap_value = existing["Kapazität"]
                        emoji_key = CAPACITY_LABEL_TO_KEY.get(cap_value, cap_value)
                        with st.container(border=True):
                            col_time, col_name, col_hall, col_btn = st.columns(
                                [2, 3, 3, 2]
                            )
                            col_time.markdown(f"🕐 {row['Uhrzeit']}")
                            col_name.markdown(f"**{row['Trainingsart']}**")
                            col_hall.markdown(
                                f"<div style='display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; margin-bottom: 1rem;'><span>📍 {row['Halle']}</span><span class='etv-day-tag' style='padding: 0.1rem 0.4rem;'>{emoji_key}</span></div>",
                                unsafe_allow_html=True,
                            )
                            col_btn.button(
                                "Auswählen",
                                key=f"done_{idx}",
                                width="stretch",
                                on_click=select_training,
                                args=(row.to_dict(),),
                            )

            # --- Missing entries from this & last week ---
            st.divider()
            st.subheader("⚠️ Fehlende Einträge")

            # Build list of all past training days in this + last week
            monday_this_week = today - timedelta(days=today.weekday())
            monday_last_week = monday_this_week - timedelta(days=7)

            # Collect missing entries grouped by date
            # { date_obj: [(day_name, row), ...] }
            missing_by_date: dict[date, list[tuple[str, pd.Series]]] = defaultdict(list)

            check_date = monday_last_week
            while check_date <= today:
                # Skip weekends and today (today is handled above)
                if check_date.weekday() < 5 and check_date != today:
                    day_name = babel.dates.format_date(check_date, "EEEE", locale=APP_LOCALE)
                    day_iso = check_date.isoformat()
                    day_trainings = config_df[config_df["Wochentag"] == day_name]

                    for _, row in day_trainings.iterrows():
                        existing = find_existing_entry(
                            data_df,
                            day_iso,
                            row["Wochentag"],
                            row["Uhrzeit"],
                            row["Trainingsart"],
                            row["Halle"],
                        )
                        if existing is None:
                            missing_by_date[check_date].append((day_name, row))

                check_date += timedelta(days=1)

            # Split into this week / last week
            this_week_dates = {
                d: entries
                for d, entries in missing_by_date.items()
                if d >= monday_this_week
            }
            last_week_dates = {
                d: entries
                for d, entries in missing_by_date.items()
                if d < monday_this_week
            }

            has_missing = bool(this_week_dates or last_week_dates)

            def render_week_section(
                label: str,
                dates: dict[date, list[tuple[str, pd.Series]]],
            ) -> None:
                if not dates:
                    return
                st.markdown(f"**{label}**")
                for d in sorted(dates.keys(), reverse=True):
                    entries = dates[d]
                    day_name = babel.dates.format_date(d, "EEEE", locale=APP_LOCALE)
                    formatted = d.strftime("%d.%m.")
                    with st.expander(
                        f"{day_name}, {formatted} — {len(entries)} offen",
                        expanded=False,
                    ):
                        for day_name, row in sorted(
                            entries, key=lambda x: x[1]["Uhrzeit"]
                        ):
                            with st.container(border=True):
                                col_time, col_name, col_hall, col_btn = st.columns(
                                    [2, 3, 3, 2]
                                )
                                col_time.markdown(f"🕐 {row['Uhrzeit']}")
                                col_name.markdown(f"**{row['Trainingsart']}**")
                                col_hall.markdown(f"📍 {row['Halle']}")
                                entry = row.to_dict()
                                entry["_override_date"] = d.isoformat()
                                col_btn.button(
                                    "Nachtragen",
                                    key=f"missing_{d.isoformat()}_{row['Uhrzeit']}_{row['Halle']}",
                                    type="primary",
                                    width="stretch",
                                    on_click=select_training,
                                    args=(entry,),
                                )

            if has_missing:
                render_week_section("Diese Woche", this_week_dates)
                render_week_section("Letzte Woche", last_week_dates)
            else:
                st.success("Alles erfasst! 🎉")

        def render_statistics(data_df, config_df, short_names):
            st.write("")  # Add some spacing

            st.subheader("Wöchentliche Auslastung")

            st.write("")
            
            # Map labels to numbers
            df = data_df.copy()
            if not df.empty:
                # Deduplicate to the latest submission per (date, slot).
                # Sort by Eingetragen am first so the last row per group is
                # truly the most recent. Fill missing timestamps (historical rows
                # without the column) with "" so they sort before any real value
                # and are never falsely treated as "latest".
                if "Eingetragen am" in df.columns:
                    df["Eingetragen am"] = df["Eingetragen am"].fillna("").astype(str)
                    df = df.sort_values("Eingetragen am", kind="stable")
                df = df.drop_duplicates(
                    subset=["Datum", "Wochentag", "Uhrzeit", "Trainingsart", "Halle"],
                    keep="last",
                )
                df["Num"] = df["Kapazität"].map(CAPACITY_TO_NUM)
            else:
                df["Num"] = pd.Series(dtype=float)

            if config_df.empty:
                st.info("Keine Stammdaten gefunden.")
                return

            # Parse "17:00 - 19:00" into decimal hours
            def parse_time_range(uhrzeit):
                uhrzeit = str(uhrzeit).strip().replace("–", "-").replace("—", "-")
                if " - " in uhrzeit:
                    parts = uhrzeit.split(" - ")
                elif "-" in uhrzeit:
                    parts = uhrzeit.split("-")
                else:
                    # Single time — assume 1-hour block
                    parts = [uhrzeit, uhrzeit]
                start_parts = parts[0].strip().split(":")
                end_parts = parts[1].strip().split(":")
                start_h = float(start_parts[0]) + float(start_parts[1]) / 60
                end_h = float(end_parts[0]) + float(end_parts[1]) / 60
                if end_h <= start_h:
                    end_h = start_h + 1  # fallback: 1-hour block
                return start_h, end_h

            config_plot = config_df.copy()
            parsed = config_plot["Uhrzeit"].apply(
                lambda x: pd.Series(parse_time_range(x), index=["StartH", "EndH"])
            )
            config_plot = pd.concat([config_plot, parsed], axis=1)

            # Global time range for y-axis
            min_hour = int(config_plot["StartH"].min())
            max_hour = int(config_plot["EndH"].max())
            if config_plot["EndH"].max() > max_hour:
                max_hour += 1

            # Aggregate capacity per individual training slot
            if not df.empty:
                agg_df = df.groupby(
                    ["Uhrzeit", "Wochentag", "Trainingsart", "Halle"]
                ).agg(Num=("Num", "mean")).reset_index()
                plot_df = pd.merge(
                    config_plot[["Wochentag", "Uhrzeit", "Trainingsart", "Halle", "StartH", "EndH"]],
                    agg_df,
                    on=["Wochentag", "Uhrzeit", "Trainingsart", "Halle"],
                    how="left",
                )
            else:
                plot_df = config_plot[
                    ["Wochentag", "Uhrzeit", "Trainingsart", "Halle", "StartH", "EndH"]
                ].copy()
                plot_df["Num"] = float("nan")

            plot_df["AuslastungLabel"] = plot_df["Num"].apply(
                lambda v: f"{v:.1f}" if pd.notna(v) else "Keine Daten"
            )
            # Fill NaN with 0 so Altair doesn't filter out the row
            plot_df["Num"] = plot_df["Num"].fillna(0)

            # Calculate midpoint for text positioning
            plot_df["MidH"] = (plot_df["StartH"] + plot_df["EndH"]) / 2
            plot_df["Halle_Kurz"] = plot_df["Halle"].map(short_names).fillna(plot_df["Halle"])
            plot_df["Label"] = plot_df["Trainingsart"] + " (" + plot_df["Halle"] + ")"

            weekday_order = [
                "Montag", "Dienstag", "Mittwoch", "Donnerstag",
                "Freitag", "Samstag", "Sonntag",
            ]
            active_weekdays = [w for w in weekday_order if w in config_plot["Wochentag"].values]
            hour_ticks = list(range(min_hour, max_hour + 1))

            # Schedule-style chart: rect spans from StartH to EndH
            rects = alt.Chart(plot_df).mark_rect(
                cornerRadius=4, stroke="white", strokeWidth=1,
            ).encode(
                x=alt.X(
                    "Label:N", 
                    title=None, 
                    axis=None,  # Hide individual column text labels
                    scale=alt.Scale(paddingInner=0.05, paddingOuter=0.05)
                ),
                y=alt.Y(
                    "StartH:Q",
                    scale=alt.Scale(domain=[min_hour, max_hour], nice=False, reverse=True),
                    axis=alt.Axis(
                        values=hour_ticks,
                        labelExpr="datum.value + ':00'",
                        title="",
                        gridColor="#eee",
                    ),
                ),
                y2="EndH:Q",
                color=alt.condition(
                    "datum.Num > 0",
                    alt.Color(
                        "Num:Q",
                        scale=alt.Scale(domain=[1, 3], range=["#2e7d32", "#fbc02d", "#c62828"]),
                        title="Auslastung (1=Leer, 3=Voll)",
                        legend=alt.Legend(orient="bottom"),
                    ),
                    alt.value("#e0e0e0"),
                ),
                tooltip=[
                    alt.Tooltip("Uhrzeit:N", title="Uhrzeit"),
                    alt.Tooltip("Trainingsart:N", title="Slot"),
                    alt.Tooltip("Halle:N", title="Location"),
                    alt.Tooltip("AuslastungLabel:N", title="Auslastung"),
                ],
            )
            
            text = alt.Chart(plot_df).mark_text(
                align='center', baseline='middle', fontSize=8, fontWeight='bold'
            ).encode(
                x=alt.X("Label:N"),
                y=alt.Y("MidH:Q", scale=alt.Scale(domain=[min_hour, max_hour], nice=False, reverse=True)),
                text="Halle_Kurz:N",
                color=alt.condition("datum.Num > 0", alt.value('white'), alt.value('#777')),
                tooltip=[
                    alt.Tooltip("Uhrzeit:N", title="Uhrzeit"),
                    alt.Tooltip("Trainingsart:N", title="Slot"),
                    alt.Tooltip("Halle:N", title="Location"),
                    alt.Tooltip("AuslastungLabel:N", title="Auslastung"),
                ]
            )

            base_chart = alt.layer(rects, text).properties(
                width=125,
                height=max(300, (max_hour - min_hour) * 80)
            )

            chart = base_chart.facet(
                column=alt.Column(
                    "Wochentag:O", 
                    sort=active_weekdays, 
                    title=None,
                    header=alt.Header(labelOrient="bottom", labelFontSize=12, labelPadding=10)
                )
            ).resolve_scale(
                x="independent"  # Ensures spacing adapts perfectly per day
            ).configure_view(
                stroke="gray",
                strokeDash=[2, 4],
                strokeOpacity=0.3,
                strokeWidth=1
            ).configure_facet(
                spacing=5
            )

            st.altair_chart(chart, width="stretch")

            st.divider()
            st.subheader("Auslastung pro Trainingseinheit")
            
            # Build Slot column for the detail chart
            config_plot["Slot"] = config_plot["Uhrzeit"] + " | " + config_plot["Trainingsart"] + " (" + config_plot["Halle"] + ")"
            if not df.empty:
                df_detail = df.copy()
                df_detail["Slot"] = df_detail["Uhrzeit"] + " | " + df_detail["Trainingsart"] + " (" + df_detail["Halle"] + ")"
                df_detail["Num"] = df_detail["Kapazität"].map(CAPACITY_TO_NUM)
            else:
                df_detail = pd.DataFrame()

            col1, col2 = st.columns(2)
            with col1:
                selected_weekday = st.selectbox("Wochentag auswählen", active_weekdays)
                
            day_config = config_plot[config_plot["Wochentag"] == selected_weekday]
            
            with col2:
                if day_config.empty:
                    st.selectbox("Training auswählen", ["Keine Trainings verfügbar"], disabled=True)
                    selected_slot = None
                else:
                    slots = day_config["Slot"].unique()
                    selected_slot = st.selectbox("Training auswählen", slots)
            
            if selected_slot:
                if df_detail.empty:
                    st.info("Noch keine Historie für dieses Training vorhanden.")
                else:
                    detail_df = df_detail[(df_detail["Wochentag"] == selected_weekday) & (df_detail["Slot"] == selected_slot)].copy()
                    if detail_df.empty:
                        st.info("Noch keine Historie für dieses Training vorhanden.")
                    else:
                        detail_df["Datum"] = pd.to_datetime(detail_df["Datum"])
                        detail_df = detail_df.sort_values("Datum")

                        # Time range selector for scalability
                        range_options = {"1 Monat": 1, "3 Monate": 3, "6 Monate": 6, "12 Monate": 12, "Alle": None}
                        selected_range = st.segmented_control(
                            "Zeitraum",
                            options=list(range_options.keys()),
                            default="3 Monate",
                            label_visibility="collapsed",
                        )
                        months = range_options.get(selected_range)
                        if months is not None:
                            cutoff = pd.Timestamp.now() - pd.DateOffset(months=months)
                            detail_df = detail_df[detail_df["Datum"] >= cutoff]

                        if detail_df.empty:
                            st.info("Keine Einträge im gewählten Zeitraum.")
                        else:
                            # Format date as dd.MM for compact ordinal labels
                            detail_df["Datum_Label"] = detail_df["Datum"].dt.strftime("%d.%m.")

                            label_expr = "datum.value == 1 ? '🟢 Noch gut Luft' : datum.value == 2 ? '🟡 Passt' : '🔴 Zu voll'"

                            line = alt.Chart(detail_df).mark_line(point=True).encode(
                                x=alt.X('Datum_Label:O', title="",
                                        sort=detail_df["Datum_Label"].tolist(),
                                        axis=alt.Axis(labelPadding=4, labelAngle=-45, labelOverlap="greedy")),
                                y=alt.Y('Num:Q',
                                        scale=alt.Scale(domain=[0.8, 3.2], zero=False, nice=False),
                                        axis=alt.Axis(values=[1, 2, 3], labelExpr=label_expr, title="")),
                                tooltip=[
                                    alt.Tooltip('Datum:T', title='Datum', format='%d.%m.%Y'),
                                    'Halle', 'Kapazität',
                                ],
                            )

                            rules = alt.Chart(pd.DataFrame({'Num': [1, 2, 3]})).mark_rule(
                                color='gray', opacity=0.3, strokeDash=[4, 4],
                            ).encode(y='Num:Q')

                            trend = (rules + line).properties(
                                height=250,
                                padding={"top": 10, "bottom": 5, "left": 5, "right": 5},
                            )
                            
                            st.altair_chart(trend, width="stretch")

        if ENABLE_STATISTICS_TAB:
            tab1, tab2 = st.tabs(["Heute Eintragen", "Statistiken & Historie"])
            with tab1:
                render_overview()
            with tab2:
                if st.session_state._cached_full_data_df is None:
                    try:
                        with st.spinner("Historie wird geladen..."):
                            st.session_state._cached_full_data_df = fetch_full_capacity(spreadsheet_id)
                    except Exception as exc:
                        if is_quota_error(exc):
                            st.error(
                                "**Google API-Limit erreicht** — Die Historie konnte nicht geladen werden.\n\n"
                                "Bitte warte ein paar Sekunden und versuche es erneut."
                            )
                        else:
                            st.error(f"Fehler beim Laden der Historie: {exc}")
                        # Leave _cached_full_data_df as None so the next tab open retries
                if st.session_state._cached_full_data_df is not None:
                    render_statistics(st.session_state._cached_full_data_df, config_df, short_names)
        else:
            render_overview()

    # =====================================================================
    # STEP 2 – Detail / Rating view
    # =====================================================================
    else:
        t = st.session_state.selected_training

        # Determine the date for this entry (today or override from missing list)
        entry_date = t.get("_override_date", today_iso)

        st.button("← Zurück zur Übersicht", on_click=clear_selection)

        st.subheader(f"{t['Trainingsart']}")
        date_label = entry_date if entry_date != today_iso else "Heute"
        st.markdown(
            f"🕐 **{t['Uhrzeit']}**  ·  📍 {t['Halle']}  ·  {t['Wochentag']} ({date_label})"
        )

        status_container = st.empty()

        st.divider()

        with status_container:
            # Check for intermediate updates using the freshly fetched cached dataframe
            existing_info = None
            if st.session_state.get("_cached_data_df") is not None:
                existing_info = find_existing_entry(
                    st.session_state._cached_data_df,
                    entry_date,
                    t["Wochentag"],
                    t["Uhrzeit"],
                    t["Trainingsart"],
                    t["Halle"],
                )

            if existing_info is not None:
                st.info(
                    f"ℹ️ **Hinweis:** Jemand anderes hat in der Zwischenzeit bereits **{existing_info['Kapazität']}** eingetragen. Du kannst diesen Wert hier überschreiben."
                )

        # Show the rating UI — always allow rating, check on submit
        st.markdown("**Wie voll ist es?**")

        capacity_choice = st.segmented_control(
            "Auslastung",
            options=list(CAPACITY_OPTIONS.keys()),
            default=list(CAPACITY_OPTIONS.keys())[0],
            label_visibility="collapsed",
            disabled=st.session_state.is_submitting,
        )

        if st.session_state.is_submitting:
            st.button(
                "⏳ Wird verarbeitet...",
                type="primary",
                width="stretch",
                disabled=True,
            )

            capacity_label = CAPACITY_OPTIONS[capacity_choice]
            try:
                # Fresh fetch at submit time to handle concurrent changes
                # (only recent data, not full history)
                fresh_df = fetch_recent_capacity(spreadsheet_id, config_df)
                st.session_state._cached_data_df = fresh_df
                existing = find_existing_entry(
                    fresh_df,
                    entry_date,
                    t["Wochentag"],
                    t["Uhrzeit"],
                    t["Trainingsart"],
                    t["Halle"],
                )

                if existing is not None:
                    # Reset submitting state before showing dialog
                    st.session_state.is_submitting = False
                    # Modal dialog handles the update
                    confirm_override_dialog(
                        existing["Kapazität"],
                        capacity_label,
                        spreadsheet_id,
                        entry_date,
                        t,
                    )
                else:
                    client = get_gspread_client()
                    data_ws = get_or_create_worksheet(
                        client, spreadsheet_id, DATA_SHEET_NAME, headers=DATA_HEADERS
                    )
                    submitted_at = submit_entry(
                        data_ws,
                        t["Wochentag"],
                        t["Uhrzeit"],
                        t["Trainingsart"],
                        t["Halle"],
                        capacity_label,
                        date_iso=entry_date,
                    )
                    # Append the new row directly into the cached DataFrame so
                    # the overview reloads instantly without a spinner.
                    new_row = pd.DataFrame([{
                        "Datum": entry_date,
                        "Wochentag": t["Wochentag"],
                        "Uhrzeit": t["Uhrzeit"],
                        "Trainingsart": t["Trainingsart"],
                        "Halle": t["Halle"],
                        "Kapazität": capacity_label,
                        "Eingetragen am": submitted_at,
                    }])
                    st.session_state._cached_data_df = pd.concat(
                        [fresh_df, new_row], ignore_index=True
                    )
                    st.session_state.pending_toast = {
                        "msg": f"**{capacity_choice}** für **{t['Trainingsart']}** ({t['Uhrzeit']}, {t['Halle']}) eingetragen!",
                    }
                    st.session_state.is_submitting = False
                    st.session_state.selected_training = None
                    st.rerun()
            except Exception as exc:
                st.session_state.is_submitting = False
                if is_quota_error(exc):
                    st.error(
                        "**Google API-Limit erreicht** — Der Eintrag konnte nicht gespeichert werden.\n\n"
                        "Bitte warte ein paar Sekunden und versuche es erneut."
                    )
                else:
                    st.error(f"**Fehler beim Eintragen** — {exc}")
        else:
            if st.button(
                "Absenden",
                type="primary",
                width="stretch",
                disabled=not capacity_choice,
            ):
                st.session_state.is_submitting = True
                st.rerun()


if __name__ == "__main__":
    main()
