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


def fetch_data(spreadsheet_id: str) -> pd.DataFrame:
    """Read the capacity data sheet into a DataFrame."""
    client = get_gspread_client()
    ws = get_or_create_worksheet(
        client, spreadsheet_id, DATA_SHEET_NAME, headers=DATA_HEADERS,
    )
    records = ws.get_all_records()
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



@st.dialog("Bestätigung: Eintrag überschreiben")
def confirm_override_dialog(existing_label, new_label, spreadsheet_id, entry_date, t):
    st.warning(f"Für diese Halle wurde bereits **{existing_label}** eingetragen.")
    st.markdown(f"Möchtest du dies wirklich mit **{new_label}** überschreiben?")
    
    col1, col2 = st.columns(2)
    if col1.button("Abbrechen", use_container_width=True):
        st.rerun()
    if col2.button("Überschreiben", type="primary", use_container_width=True):
        client = get_gspread_client()
        data_ws = get_or_create_worksheet(client, spreadsheet_id, DATA_SHEET_NAME, headers=DATA_HEADERS)
        update_entry(
            data_ws, entry_date,
            t["Wochentag"], t["Uhrzeit"],
            t["Trainingsart"], t["Halle"],
            new_label,
        )
        st.session_state.selected_training = None
        st.session_state._data_stale = True
        st.rerun()

def main() -> None:
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
        button[kind="primary"] {
            background-color: #DC0D15 !important;
            border-color: #DC0D15 !important;
            color: white !important;
        }
        button[kind="primary"]:hover {
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
        }

        /* Hide sidebar */
        [data-testid="stSidebar"] { display: none; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    def clear_selection():
        st.session_state.selected_training = None
        st.session_state.edit_mode = False
        st.session_state._data_stale = True

    # --- Session state defaults ---
    if "selected_training" not in st.session_state:
        st.session_state.selected_training = None
    if "edit_mode" not in st.session_state:
        st.session_state.edit_mode = False
    if "_data_stale" not in st.session_state:
        st.session_state._data_stale = True
    if "_cached_data_df" not in st.session_state:
        st.session_state._cached_data_df = None

    # --- Determine today (needed for header) ---
    today = date.today()
    today_iso = today.isoformat()
    today_weekday = WEEKDAY_MAP[today.weekday()]

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
            <div class="etv-day-tag">Heute ist <strong>{today_weekday}</strong></div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    spreadsheet_id = os.getenv("GOOGLE_SHEET_ID", "")

    def select_training(entry):
        if spreadsheet_id:
            st.session_state._cached_data_df = fetch_data(spreadsheet_id)
        st.session_state.selected_training = entry
        st.session_state.edit_mode = False

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
    # =====================================================================
    # STEP 1 – Training selection (landing page)
    # =====================================================================
    if st.session_state.selected_training is None:
        def render_overview():
            data_df = fetch_data(spreadsheet_id)
            # Store initially so we have it if needed
            st.session_state._cached_data_df = data_df

            day_df = config_df[config_df["Wochentag"] == today_weekday]

            st.markdown("Ihr helft uns dabei, die Auslastung unserer Hallen zu bewerten. Gebt einfach an wie voll ihr die Halle empfindet, wenn ihr da seid.")

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
                        with st.container(border=True):
                            col_time, col_name, col_hall, col_btn = st.columns([2, 3, 3, 2])
                            col_time.markdown(f"🕐 {row['Uhrzeit']}")
                            col_name.markdown(f"**{row['Trainingsart']}**")
                            col_hall.markdown(f"📍 {row['Halle']}")
                            col_btn.button(
                                "Auswählen",
                                key=f"open_{idx}",
                                type="primary",
                                use_container_width=True,
                                on_click=select_training,
                                args=(row.to_dict(),)
                            )

                # --- Already submitted ---
                if submitted_trainings:
                    st.subheader("✅ Bereits erfasst")
                    for idx, row, existing in submitted_trainings:
                        cap_value = existing["Kapazität"]
                        emoji_key = CAPACITY_LABEL_TO_KEY.get(cap_value, cap_value)
                        with st.container(border=True):
                            col_time, col_name, col_hall, col_btn = st.columns([2, 3, 3, 2])
                            col_time.markdown(f"🕐 {row['Uhrzeit']}")
                            col_name.markdown(f"**{row['Trainingsart']}**")
                            col_hall.markdown(f"<div style='display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; margin-bottom: 1rem;'><span>📍 {row['Halle']}</span><span class='etv-day-tag' style='padding: 0.1rem 0.4rem;'>{emoji_key}</span></div>", unsafe_allow_html=True)
                            col_btn.button(
                                "Auswählen",
                                key=f"done_{idx}",
                                use_container_width=True,
                                on_click=select_training,
                                args=(row.to_dict(),)
                            )

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
                            with st.container(border=True):
                                col_time, col_name, col_hall, col_btn = st.columns([2, 3, 3, 2])
                                col_time.markdown(f"🕐 {row['Uhrzeit']}")
                                col_name.markdown(f"**{row['Trainingsart']}**")
                                col_hall.markdown(f"📍 {row['Halle']}")
                                entry = row.to_dict()
                                entry["_override_date"] = d.isoformat()
                                col_btn.button(
                                    "Nachtragen",
                                    key=f"missing_{d.isoformat()}_{row['Uhrzeit']}_{row['Halle']}",
                                    type="primary",
                                    use_container_width=True,
                                    on_click=select_training,
                                    args=(entry,)
                                )

            if has_missing:
                render_week_section("Diese Woche", this_week_dates)
                render_week_section("Letzte Woche", last_week_dates)
            else:
                st.success("Alles erfasst! 🎉")



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

        # Show the rating UI — always allow rating, check on submit
        st.markdown("**Wie voll ist es?**")

        capacity_choice = st.segmented_control(
            "Auslastung",
            options=list(CAPACITY_OPTIONS.keys()),
            default=list(CAPACITY_OPTIONS.keys())[0],
            label_visibility="collapsed",
        )

        submit_btn = st.button("Absenden", type="primary", use_container_width=True, disabled=not capacity_choice)

        with status_container:
            # Check for intermediate updates using the freshly fetched cached dataframe
            existing = None
            if st.session_state.get("_cached_data_df") is not None:
                existing = find_existing_entry(
                    st.session_state._cached_data_df, entry_date,
                    t["Wochentag"], t["Uhrzeit"],
                    t["Trainingsart"], t["Halle"],
                )

            if existing is not None:
                st.info(f"ℹ️ **Hinweis:** Jemand anderes hat in der Zwischenzeit bereits **{existing['Kapazität']}** eingetragen. Du kannst diesen Wert hier überschreiben.")

        if submit_btn:
            capacity_label = CAPACITY_OPTIONS[capacity_choice]
            try:
                # Fresh fetch at submit time to handle concurrent changes
                fresh_df = fetch_data(spreadsheet_id)
                st.session_state._cached_data_df = fresh_df
                existing = find_existing_entry(
                    fresh_df, entry_date,
                    t["Wochentag"], t["Uhrzeit"],
                    t["Trainingsart"], t["Halle"],
                )

                if existing is not None:
                    # Modal dialog handles the update
                    confirm_override_dialog(existing["Kapazität"], capacity_label, spreadsheet_id, entry_date, t)
                else:
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
                    st.session_state._data_stale = True
                    st.rerun()
            except Exception as exc:
                st.error(f"Fehler beim Eintragen: {exc}")




if __name__ == "__main__":
    main()
