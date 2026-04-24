import streamlit as st
import gspread
from google.oauth2.service_account import Credentials
import pandas as pd
from collections import defaultdict
from datetime import date, datetime, timedelta
from dotenv import load_dotenv
import json
import os

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

# Traffic-light capacity options
CAPACITY_OPTIONS = {
    "🟢 Noch gut Luft": "Noch gut Luft",
    "🟡 Passt": "Passt",
    "🔴 Zu voll": "Zu voll",
}

CONFIG_HEADERS = [
    "Wochentag", "Uhrzeit", "Trainingsart", "Halle", "Max. Kapazität",
]

DATA_HEADERS = [
    "Datum", "Wochentag", "Uhrzeit", "Trainingsart", "Halle", "Kapazität",
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


@st.cache_data(ttl=300, show_spinner="Trainingsdaten werden geladen...")
def fetch_config(spreadsheet_id: str) -> pd.DataFrame:
    """Read the Stammdaten (base config) sheet."""
    client = get_gspread_client()
    ws = get_or_create_worksheet(
        client,
        spreadsheet_id,
        CONFIG_SHEET_NAME,
        headers=CONFIG_HEADERS,
    )
    records = ws.get_all_records()
    if not records:
        return pd.DataFrame(
            columns=CONFIG_HEADERS
        )
    return pd.DataFrame(records)


def fetch_data(worksheet: gspread.Worksheet) -> pd.DataFrame:
    """Read the capacity data sheet into a DataFrame."""
    records = worksheet.get_all_records()
    if not records:
        return pd.DataFrame(columns=DATA_HEADERS)
    return pd.DataFrame(records)


def find_existing_entry(
    data_df: pd.DataFrame,
    today_iso: str,
    weekday: str,
    time_slot: str,
    training_type: str,
    hall: str,
) -> pd.Series | None:
    """Return the row for today's entry matching the training slot, or None."""
    mask = (
        (data_df["Datum"].astype(str) == today_iso)
        & (data_df["Wochentag"] == weekday)
        & (data_df["Uhrzeit"] == time_slot)
        & (data_df["Trainingsart"] == training_type)
        & (data_df["Halle"] == hall)
    )
    matches = data_df[mask]
    if matches.empty:
        return None
    return matches.iloc[-1]  # latest entry if duplicates


def submit_entry(
    worksheet: gspread.Worksheet,
    weekday: str,
    time_slot: str,
    training_type: str,
    hall: str,
    capacity_label: str,
    date_iso: str | None = None,
) -> None:
    """Append a new capacity entry: Datum, Wochentag, Uhrzeit, Trainingsart, Halle, Kapazität."""
    entry_date = date_iso or date.today().isoformat()
    worksheet.append_row(
        [entry_date, weekday, time_slot, training_type, hall, capacity_label]
    )


def update_entry(
    worksheet: gspread.Worksheet,
    today_iso: str,
    weekday: str,
    time_slot: str,
    training_type: str,
    hall: str,
    capacity_label: str,
) -> None:
    """Find and update an existing row matching the training slot for today."""
    all_values = worksheet.get_all_values()
    # Search from bottom to top to find the latest matching row
    for row_idx in range(len(all_values) - 1, 0, -1):
        row = all_values[row_idx]
        if (
            len(row) >= 6
            and row[0] == today_iso
            and row[1] == weekday
            and row[2] == time_slot
            and row[3] == training_type
            and row[4] == hall
        ):
            # row_idx is 0-based, worksheet rows are 1-based
            worksheet.update_cell(row_idx + 1, 6, capacity_label)
            return
    # Fallback: append if not found (shouldn't happen)
    worksheet.append_row(
        [today_iso, weekday, time_slot, training_type, hall, capacity_label]
    )


# Map Python weekday() (0=Mon) to German names
WEEKDAY_MAP = {
    0: "Montag",
    1: "Dienstag",
    2: "Mittwoch",
    3: "Donnerstag",
    4: "Freitag",
    5: "Samstag",
    6: "Sonntag",
}

# Reverse lookup: capacity label → emoji key
CAPACITY_LABEL_TO_KEY = {v: k for k, v in CAPACITY_OPTIONS.items()}


# ---------------------------------------------------------------------------
# Streamlit UI
# ---------------------------------------------------------------------------


def main() -> None:
    st.set_page_config(
        page_title="ETV Hallenkapazität",
        page_icon="🏸",
        layout="centered",
    )

    # --- Custom CSS: full-width segmented control with equal segments ---
    st.markdown(
        """
        <style>
        /* Override the parent container's fit-content width */
        div[data-testid="stElementContainer"]:has(> div[data-testid="stButtonGroup"]) {
            width: 100% !important;
        }
        /* Make the segmented control span full width with spacing */
        div[data-testid="stButtonGroup"] {
            width: 100%;
            margin-top: 1rem;
            margin-bottom: 1.5rem;
        }
        /* Stretch the inner button-group container */
        div[data-testid="stButtonGroup"] > div[data-baseweb="button-group"] {
            width: 100% !important;
            display: flex !important;
        }
        /* Each segment equal width */
        div[data-testid="stButtonGroup"] > div[data-baseweb="button-group"] > button {
            flex: 1 1 0% !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    # --- Session state defaults ---
    if "selected_training" not in st.session_state:
        st.session_state.selected_training = None
    if "edit_mode" not in st.session_state:
        st.session_state.edit_mode = False

    st.title("🏸 ETV Badminton – Hallenkapazität")

    # --- Google Sheet config ---
    spreadsheet_id = os.getenv("GOOGLE_SHEET_ID", "")

    if not spreadsheet_id:
        st.warning(
            "Bitte setze die Umgebungsvariable `GOOGLE_SHEET_ID` "
            "mit der ID deines Google Sheets."
        )
        st.stop()

    # --- Load base config from Stammdaten ---
    config_df = fetch_config(spreadsheet_id)

    if config_df.empty:
        st.warning(
            f"Das Blatt **{CONFIG_SHEET_NAME}** ist leer. "
            "Bitte fülle es mit den Trainingsdaten "
            "(Wochentag, Uhrzeit, Trainingsart, Halle, Max. Kapazität)."
        )
        st.stop()

    # --- Connect to data sheet ---
    client = get_gspread_client()
    data_ws = get_or_create_worksheet(
        client,
        spreadsheet_id,
        DATA_SHEET_NAME,
        headers=DATA_HEADERS,
    )

    # --- Determine today ---
    today = date.today()
    today_iso = today.isoformat()
    today_weekday = WEEKDAY_MAP[today.weekday()]

    # --- Fetch existing entries ---
    data_df = fetch_data(data_ws)

    # =====================================================================
    # STEP 1 – Training selection (landing page)
    # =====================================================================
    if st.session_state.selected_training is None:
        st.markdown(f"Heute ist **{today_weekday}**")

        day_df = config_df[config_df["Wochentag"] == today_weekday]

        if day_df.empty:
            st.info(f"Am {today_weekday} gibt es keine Trainings.")
        else:
            # Split trainings into open / already submitted
            open_trainings = []
            submitted_trainings = []

            for idx, row in day_df.iterrows():
                existing = find_existing_entry(
                    data_df, today_iso,
                    row["Wochentag"], row["Uhrzeit"],
                    row["Trainingsart"], row["Halle"],
                )
                if existing is not None:
                    submitted_trainings.append((idx, row, existing))
                else:
                    open_trainings.append((idx, row))

            # --- Open trainings ---
            if open_trainings:
                st.subheader("📋 Noch offen")
                for idx, row in open_trainings:
                    if st.button(
                        f"🕐 {row['Uhrzeit']}  ·  {row['Trainingsart']}  ·  {row['Halle']}",
                        key=f"open_{idx}",
                        use_container_width=True,
                    ):
                        st.session_state.selected_training = row.to_dict()
                        st.session_state.edit_mode = False
                        st.rerun()

            # --- Already submitted ---
            if submitted_trainings:
                st.subheader("✅ Bereits erfasst")
                for idx, row, existing in submitted_trainings:
                    cap_value = existing["Kapazität"]
                    emoji_key = CAPACITY_LABEL_TO_KEY.get(cap_value, cap_value)
                    if st.button(
                        f"🕐 {row['Uhrzeit']}  ·  {row['Trainingsart']}  ·  {emoji_key}",
                        key=f"done_{idx}",
                        use_container_width=True,
                    ):
                        st.session_state.selected_training = row.to_dict()
                        st.session_state.edit_mode = False
                        st.rerun()

        # --- Missing entries from this & last week ---
        st.divider()
        st.subheader("⚠️ Fehlende Einträge")

        # Build list of all past training days in this + last week
        monday_this_week = today - timedelta(days=today.weekday())
        monday_last_week = monday_this_week - timedelta(days=7)
        friday_last_week = monday_last_week + timedelta(days=4)

        # Collect missing entries grouped by date
        # { date_obj: [(day_name, row), ...] }
        missing_by_date: dict[date, list[tuple[str, pd.Series]]] = defaultdict(list)

        check_date = monday_last_week
        while check_date <= today:
            # Skip weekends and today (today is handled above)
            if check_date.weekday() < 5 and check_date != today:
                day_name = WEEKDAY_MAP[check_date.weekday()]
                day_iso = check_date.isoformat()
                day_trainings = config_df[config_df["Wochentag"] == day_name]

                for _, row in day_trainings.iterrows():
                    existing = find_existing_entry(
                        data_df, day_iso,
                        row["Wochentag"], row["Uhrzeit"],
                        row["Trainingsart"], row["Halle"],
                    )
                    if existing is None:
                        missing_by_date[check_date].append((day_name, row))

            check_date += timedelta(days=1)

        # Split into this week / last week
        this_week_dates = {
            d: entries for d, entries in missing_by_date.items()
            if d >= monday_this_week
        }
        last_week_dates = {
            d: entries for d, entries in missing_by_date.items()
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
                day_name = WEEKDAY_MAP[d.weekday()]
                formatted = d.strftime("%d.%m.")
                with st.expander(
                    f"{day_name}, {formatted} — {len(entries)} offen",
                    expanded=False,
                ):
                    for day_name, row in sorted(entries, key=lambda x: x[1]["Uhrzeit"]):
                        if st.button(
                            f"🕐 {row['Uhrzeit']}  ·  {row['Trainingsart']}  ·  {row['Halle']}",
                            key=f"missing_{d.isoformat()}_{row['Uhrzeit']}_{row['Halle']}",
                            use_container_width=True,
                        ):
                            entry = row.to_dict()
                            entry["_override_date"] = d.isoformat()
                            st.session_state.selected_training = entry
                            st.session_state.edit_mode = False
                            st.rerun()

        if has_missing:
            render_week_section("Diese Woche", this_week_dates)
            render_week_section("Letzte Woche", last_week_dates)
        else:
            st.success("Alles erfasst! 🎉")

    # =====================================================================
    # STEP 2 – Detail / Rating view
    # =====================================================================
    else:
        t = st.session_state.selected_training

        # Determine the date for this entry (today or override from missing list)
        entry_date = t.get("_override_date", today_iso)

        if st.button("← Zurück zur Übersicht"):
            st.session_state.selected_training = None
            st.session_state.edit_mode = False
            st.rerun()

        st.subheader(f"{t['Trainingsart']}")
        date_label = entry_date if entry_date != today_iso else "Heute"
        st.markdown(
            f"🕐 **{t['Uhrzeit']}**  ·  📍 {t['Halle']}  ·  {t['Wochentag']} ({date_label})"
        )

        # Check if entry exists for this date
        existing = find_existing_entry(
            data_df, entry_date,
            t["Wochentag"], t["Uhrzeit"],
            t["Trainingsart"], t["Halle"],
        )

        st.divider()

        # ----- No entry yet → new submission -----
        if existing is None:
            st.markdown("**Wie voll ist es?**")

            capacity_choice = st.segmented_control(
                "Auslastung",
                options=list(CAPACITY_OPTIONS.keys()),
                default=list(CAPACITY_OPTIONS.keys())[0],
                label_visibility="collapsed",
            )

            if st.button("Absenden", type="primary", use_container_width=True, disabled=not capacity_choice):
                capacity_label = CAPACITY_OPTIONS[capacity_choice]
                try:
                    submit_entry(
                        data_ws,
                        t["Wochentag"], t["Uhrzeit"],
                        t["Trainingsart"], t["Halle"],
                        capacity_label,
                        date_iso=entry_date,
                    )
                    st.success(
                        f"✅ **{capacity_choice}** für **{t['Trainingsart']}** "
                        f"({t['Uhrzeit']}, {t['Halle']}) eingetragen!"
                    )
                    st.session_state.selected_training = None
                    st.rerun()
                except Exception as exc:
                    st.error(f"Fehler beim Eintragen: {exc}")

        # ----- Entry exists → read-only or edit mode -----
        else:
            cap_value = existing["Kapazität"]
            emoji_key = CAPACITY_LABEL_TO_KEY.get(cap_value, cap_value)

            if not st.session_state.edit_mode:
                # Read-only view
                st.markdown(f"Aktuelle Meldung: **{emoji_key}**")
                if st.button("✏️ Bearbeiten", use_container_width=True):
                    st.session_state.edit_mode = True
                    st.rerun()
            else:
                # Edit mode
                st.markdown("**Neue Auslastung wählen:**")

                # Pre-select current value
                options = list(CAPACITY_OPTIONS.keys())
                default_val = emoji_key if emoji_key in options else options[0]

                capacity_choice = st.segmented_control(
                    "Auslastung",
                    options=options,
                    default=default_val,
                    label_visibility="collapsed",
                )

                col1, col2 = st.columns(2)
                with col1:
                    if st.button("Abbrechen", use_container_width=True):
                        st.session_state.edit_mode = False
                        st.rerun()
                with col2:
                    if st.button("Speichern", type="primary", use_container_width=True, disabled=not capacity_choice):
                        capacity_label = CAPACITY_OPTIONS[capacity_choice]
                        try:
                            update_entry(
                                data_ws, entry_date,
                                t["Wochentag"], t["Uhrzeit"],
                                t["Trainingsart"], t["Halle"],
                                capacity_label,
                            )
                            st.success(
                                f"✅ Aktualisiert: **{capacity_choice}** für "
                                f"**{t['Trainingsart']}**"
                            )
                            st.session_state.edit_mode = False
                            st.session_state.selected_training = None
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Fehler beim Aktualisieren: {exc}")


if __name__ == "__main__":
    main()

