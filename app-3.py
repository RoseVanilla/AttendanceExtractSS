import json
import base64
import re
import requests
import streamlit as st
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# ==========================================
# CONFIGURATION & CONSTANTS
# ==========================================
# A-Level Sheet IDs
A_LEVEL_ATTENDANCE_SHEET_ID = "1taKxfTBnASlDI3dYJEYzFPppkjUr3-zxBgEnlpaigeU"
A_LEVEL_ROSTER_SHEET_ID = "17ulfV3Ecp-UlHXLOZT4bfKovy7B5FCPZ3efUkdZnM-w"

# O-Level Sheet IDs
O_LEVEL_ATTENDANCE_SHEET_ID = "1B_HqYmzBzL9LR0WQ_zp30gFcrZvDH3ED5bP6b-jFmT8"
O_LEVEL_ROSTER_SHEET_ID = "1Q6Wm47K9buwjh56WVTk8mGEkNqpCkC_WrXe5my1Sr_s"

OCR_SPACE_API_KEY = "K83980812088957"

# Level Configuration Mapping
LEVEL_CONFIGS = {
    "O-Level": {
        "roster_sheet_id": O_LEVEL_ROSTER_SHEET_ID,
        "roster_range": "A1:Z1000",
        "id_col_idx": 3,    # Column D
        "name_col_idx": 1,  # Column B
        "data_start_row": 1,  # Row 2 (0-indexed = 1)
        "attendance_sheet_id": O_LEVEL_ATTENDANCE_SHEET_ID,
        "attendance_tab_name": None  # Targets Tab 2 (Index 1) or Tab 1
    },
    "A-Level": {
        "roster_sheet_id": A_LEVEL_ROSTER_SHEET_ID,
        "roster_range": "A1:B1000",
        "id_col_idx": 0,    # Column A
        "name_col_idx": 1,  # Column B
        "data_start_row": 1,  # Row 2 (0-indexed = 1)
        "attendance_sheet_id": A_LEVEL_ATTENDANCE_SHEET_ID,
        "attendance_tab_name": None  # Targets Tab 2 (Index 1) or Tab 1
    }
}

HOSTS = [
    "BOULES Ramzy", "Farida Fayez", "Farah Ashraf", "Judy Hassanien",
    "Parthinia Mazouz", "Nada Wael", "Judi Ziad", "Rivana Aly",
    "Menna Amr", "Batool Khaled", "Jaidaa Gomaa", "Lobna Mohamed"
]

# Hosts who are recognised as hosts (so they are skipped, never "unmatched"),
# but are NOT written to row 4 and NOT shown in the co-host list.
UNRECORDED_HOSTS = ["BOULES Ramzy"]

# Cleaned AIS variants: Fully enclosed in brackets or unbracketed only
AIS_VARIANTS = [
    "(WITH MR BOULES IN CLASS)",
    "WITH MR BOULES IN CLASS",
    "(WITH MR BOULES)",
    "WITH MR BOULES",
    "(AIS STUDENT)",
    "AIS STUDENT",
    "(AIS)",
    "AIS"
]


# ==========================================
# HELPER FUNCTIONS
# ==========================================
def column_to_letter(col):
    """Convert 1-based column index to Sheet letter string (e.g., 2 -> B, 27 -> AA)."""
    letter = ""
    while col > 0:
        temp = (col - 1) % 26
        letter = chr(temp + 65) + letter
        col = (col - temp - 1) // 26
    return letter


def clean_ocr_lines(raw_lines):
    cleaned_lines = []
    for line in raw_lines:
        line = line.strip()
        if not line:
            continue

        # Skip standalone avatar initials (1 or 2 letters only)
        if re.match(r"^[A-Za-z]{1,2}$", line):
            continue

        # Strip Zoom host/co-host tags if prepended
        line = re.sub(r"^(H|CH|\(Host\)|\(Co-host\))\s+", "", line, flags=re.IGNORECASE)

        # Strip 1-2 character prefix followed by space
        words = line.split()
        if len(words) > 1 and len(words[0]) <= 2:
            words = words[1:]
        line = " ".join(words)

        cleaned_lines.append(line)
    return cleaned_lines


def find_ais_variant(text_upper):
    """Return the AIS variant if the line starts or ends with it, else None.
    Plain-letter variants (e.g. 'AIS') must not be glued to other letters,
    so names like 'Aisha' or 'Kais' are not mistaken for AIS tags."""
    for v in AIS_VARIANTS:
        if text_upper.startswith(v):
            nxt = text_upper[len(v):len(v) + 1]
            if not (v[-1].isalpha() and nxt.isalpha()):
                return v
        if text_upper.endswith(v):
            prev = text_upper[:len(text_upper) - len(v)][-1:]
            if not (v[0].isalpha() and prev.isalpha()):
                return v
    return None


def get_sheets_service():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    secret_data = st.secrets["gcp_service_account"]
    if isinstance(secret_data, str):
        info = json.loads(secret_data)
    else:
        info = dict(secret_data)

    if "private_key" in info and "\\n" in info["private_key"]:
        info["private_key"] = info["private_key"].replace("\\n", "\n")

    creds = Credentials.from_service_account_info(info, scopes=scopes)
    return build("sheets", "v4", credentials=creds)


def get_column_write_info(sheets, spreadsheet_id, sheet_name, col_letter):
    res = sheets.values().get(
        spreadsheetId=spreadsheet_id,
        range=f"'{sheet_name}'!{col_letter}1:{col_letter}500"
    ).execute()
    rows = res.get("values", [])

    existing_entries = []
    last_filled_row = 6  # Rows 1-6 are headers/metadata

    for idx, row in enumerate(rows, start=1):
        val = row[0].strip() if row else ""
        if val:
            last_filled_row = idx
            if idx >= 7:
                existing_entries.append(val)

    start_write_row = max(7, last_filled_row + 1)
    return existing_entries, start_write_row


# ==========================================
# MAIN OCR PROCESSING ENGINE
# ==========================================
def process_zoom_ocr_attendance(uploaded_files, target_col_letter, selected_level="O-Level"):
    service = get_sheets_service()
    sheets = service.spreadsheets()

    config = LEVEL_CONFIGS.get(selected_level, LEVEL_CONFIGS["O-Level"])

    # STEP A: Read Roster from configured Roster Sheet ID
    roster_res = sheets.values().get(
        spreadsheetId=config["roster_sheet_id"],
        range=config["roster_range"]
    ).execute()
    master_rows = roster_res.get("values", [])

    registered_students = []
    id_idx = config["id_col_idx"]
    name_idx = config["name_col_idx"]
    start_row = config["data_start_row"]

    if len(master_rows) > start_row:
        for row in master_rows[start_row:]:
            s_id = row[id_idx].strip().upper() if len(row) > id_idx and row[id_idx] else ""
            s_name = row[name_idx].strip() if len(row) > name_idx and row[name_idx] else ""
            if s_id:
                registered_students.append({"id": s_id, "name": s_name})

    valid_ids = [s["id"] for s in registered_students]

    # STEP B: Target Tab Setup
    if config["attendance_tab_name"]:
        target_sheet_name = config["attendance_tab_name"]
    else:
        meta = sheets.get(spreadsheetId=config["attendance_sheet_id"]).execute()
        sheet_list = meta.get("sheets", [])
        target_sheet_name = (
            sheet_list[1]["properties"]["title"]
            if len(sheet_list) > 1
            else sheet_list[0]["properties"]["title"]
        )

    # STEP C: Inspect Selected Column for Append Position & Existing Entries
    existing_col_values, start_write_row = get_column_write_info(
        sheets, config["attendance_sheet_id"], target_sheet_name, target_col_letter
    )

    # STEP D: Process Uploaded Screenshots via OCR Space API
    raw_lines = []
    for file in uploaded_files:
        file_bytes = file.read()
        base64_img = base64.b64encode(file_bytes).decode("utf-8")

        payload = {
            "apikey": OCR_SPACE_API_KEY,
            "language": "eng",
            "ocrengine": "2",
            "isTable": "true",
            "base64Image": f"data:image/png;base64,{base64_img}"
        }

        res = requests.post("https://api.ocr.space/parse/image", data=payload).json()
        if "ParsedResults" in res and res["ParsedResults"]:
            for result in res["ParsedResults"]:
                lines = [l.strip() for l in result.get("ParsedText", "").splitlines() if l.strip()]
                raw_lines.extend(lines)

    cleaned_lines = clean_ocr_lines(raw_lines)

    # STEP E: Host Extraction & Row 4 Header Update
    matched_hosts = []
    for line in cleaned_lines:
        for h in HOSTS:
            if h.upper() in line.upper():
                # Recognised as a host (skipped in Step F) but never written to row 4
                if h in UNRECORDED_HOSTS:
                    continue
                ap = h
                if h == "Batool Khaled":
                    ap = "Batool "
                elif " " in ap:
                    ap = ap.split(" ")[0]
                if ap not in matched_hosts:
                    matched_hosts.append(ap)

    if matched_hosts:
        host_header_text = " & ".join(matched_hosts)
        sheets.values().update(
            spreadsheetId=config["attendance_sheet_id"],
            range=f"'{target_sheet_name}'!{target_col_letter}4",
            valueInputOption="USER_ENTERED",
            body={"values": [[host_header_text]]}
        ).execute()

    # STEP F: Participant Matching Logic
    new_students_count = 0
    ais_count = 0
    newcomers_count = 0

    regular_updates = []
    ais_updates = []
    newcomer_updates = []
    unmatched_participants = []
    found_by_name = []

    for raw_part in raw_lines:
        raw_trimmed = raw_part.strip()
        if not raw_trimmed or re.match(r"^[A-Za-z]{1,2}$", raw_trimmed):
            continue

        is_host = any(h.upper() in raw_trimmed.upper() for h in HOSTS)

        # ---------------------------------------------------------
        # 1. VARIABLE-LENGTH ID CHECK (RAW LINE, SPACES REMOVED)
        # ---------------------------------------------------------
        matched_id = None
        raw_compact = raw_trimmed.upper().replace(" ", "")

        # 1a. Exact ID check against every roster ID
        for v_id in valid_ids:
            v_id_compact = v_id.upper().replace(" ", "")
            if v_id_compact in raw_compact:
                matched_id = v_id
                break

        # 1b. O <-> 0 swap fallback (only if no exact match was found)
        if not matched_id:
            raw_swapped = raw_compact.replace('O', '0')
            for v_id in valid_ids:
                v_id_compact = v_id.upper().replace(" ", "")
                if v_id_compact.replace('O', '0') in raw_swapped:
                    matched_id = v_id
                    break

        if matched_id:
            if matched_id not in existing_col_values:
                new_students_count += 1
                existing_col_values.append(matched_id)
                regular_updates.append([matched_id])
            continue  # ID found! Skip cleaning and Steps 2-4 for this line.

        # Hosts / co-hosts are not students: skip them so they never land in "unmatched"
        if is_host:
            continue

        # ---------------------------------------------------------
        # CLEAN THE LINE FOR STEPS 2, 3, & 4
        # ---------------------------------------------------------
        part = re.sub(r"^(H|CH|\(Host\)|\(Co-host\))\s+", "", raw_trimmed, flags=re.IGNORECASE)
        words = part.split()
        if len(words) > 1 and len(words[0]) <= 2:
            part = " ".join(words[1:])

        # ---------------------------------------------------------
        # 2. AIS CHECK (STARTS WITH / ENDS WITH + NOISE STRIPPING)
        # ---------------------------------------------------------
        matched_ais = find_ais_variant(part.upper())
        if matched_ais:
            ais_count += 1
            clean_name = re.sub(re.escape(matched_ais), "", part, flags=re.IGNORECASE)
            clean_name = re.sub(r"[\(\)]", "", clean_name)
            clean_name = re.sub(r"\s*(?:[%&]|\b(?:e|TA)\b).*$", "", clean_name, flags=re.IGNORECASE).strip()
            clean_name = re.sub(r"^[^a-zA-Z0-9]+|[^a-zA-Z0-9]+$", "", clean_name).strip()

            formatted = f"AIS {clean_name}"
            if formatted not in existing_col_values:
                new_students_count += 1
                existing_col_values.append(formatted)
                ais_updates.append([formatted])
            continue

        # ---------------------------------------------------------
        # 3. NEWCOMER CHECK
        # ---------------------------------------------------------
        if part.upper().endswith(("NEWCOMER", "NEW COMER")):
            newcomers_count += 1
            if part not in existing_col_values:
                new_students_count += 1
                existing_col_values.append(part)
                newcomer_updates.append([part])
            continue

        # ---------------------------------------------------------
        # 4. TOKENIZED STRICT FIRST-NAME FALLBACK MATCHING
        # ---------------------------------------------------------
        matched_id = None
        if not is_host:
            words = [w for w in part.split() if len(w) > 0]
            if words and any(char.isdigit() for char in words[0]):
                words.pop(0)

            if words:
                w1 = words[0].upper()

                # STRICT FIRST-NAME MATCH
                candidate_rows = [
                    s for s in registered_students
                    if s["name"].strip() and s["name"].strip().upper().split()[0] == w1
                ]

                if len(candidate_rows) == 1:
                    matched_id = candidate_rows[0]["id"]
                elif len(candidate_rows) > 1 and len(words) > 1:
                    for k in range(1, len(words)):
                        wk = words[k].upper()
                        refined = [s for s in candidate_rows if wk in s["name"].upper()]
                        if len(refined) == 1:
                            matched_id = refined[0]["id"]
                            break
                        elif len(refined) > 1:
                            candidate_rows = refined

        if matched_id:
            log_entry = f"{part} ➔ ID: {matched_id}"
            if log_entry not in found_by_name:
                found_by_name.append(log_entry)

            if matched_id not in existing_col_values:
                new_students_count += 1
                existing_col_values.append(matched_id)
                regular_updates.append([matched_id])
        else:
            if part and len(part.strip()) > 2 and part not in unmatched_participants:
                unmatched_participants.append(part)

    # STEP G: Write New Rows to Selected Column
    current_row = start_write_row

    if regular_updates:
        end_row = current_row + len(regular_updates) - 1
        sheets.values().update(
            spreadsheetId=config["attendance_sheet_id"],
            range=f"'{target_sheet_name}'!{target_col_letter}{current_row}:{target_col_letter}{end_row}",
            valueInputOption="USER_ENTERED",
            body={"values": regular_updates}
        ).execute()
        current_row = end_row + 3  # Leave 2 blank rows

    if ais_updates:
        end_row = current_row + len(ais_updates) - 1
        sheets.values().update(
            spreadsheetId=config["attendance_sheet_id"],
            range=f"'{target_sheet_name}'!{target_col_letter}{current_row}:{target_col_letter}{end_row}",
            valueInputOption="USER_ENTERED",
            body={"values": ais_updates}
        ).execute()
        current_row = end_row + 3  # Leave 2 blank rows

    if newcomer_updates:
        end_row = current_row + len(newcomer_updates) - 1
        sheets.values().update(
            spreadsheetId=config["attendance_sheet_id"],
            range=f"'{target_sheet_name}'!{target_col_letter}{current_row}:{target_col_letter}{end_row}",
            valueInputOption="USER_ENTERED",
            body={"values": newcomer_updates}
        ).execute()

    return {
        "target_column": target_col_letter,
        "start_row": start_write_row,
        "total_detected": len(cleaned_lines),
        "new_students_count": new_students_count,
        "ais_count": ais_count,
        "newcomers_count": newcomers_count,
        "matched_hosts": matched_hosts,
        "found_by_name": found_by_name,
        "unmatched_count": len(unmatched_participants),
        "unmatched_list": unmatched_participants
    }


# ==========================================
# STREAMLIT USER INTERFACE
# ==========================================
st.set_page_config(page_title="Zoom Attendance OCR", page_icon="📋")
st.title("Zoom Attendance OCR Processor")

selected_level = st.radio("Select Class Level:", ["O-Level", "A-Level"], index=0, horizontal=True)
col_input = st.text_input("Target Column Letter (e.g., B, C, D, AA):", value="").strip().upper()

uploaded_images = st.file_uploader(
    "Upload Zoom Screenshot(s)",
    type=["jpg", "jpeg", "png"],
    accept_multiple_files=True
)

if st.button("Process Attendance"):
    num_screenshots = len(uploaded_images)
    with st.spinner(f"Processing {num_screenshots} screenshot(s) for {selected_level} to Column {col_input}..."):
        res = process_zoom_ocr_attendance(uploaded_images, col_input, selected_level)

    st.success(f"[{selected_level}] Attendance Logged in Column {res['target_column']}!")
    st.write(f"• **Total Participants Detected:** {res['total_detected']}")
    st.write(f"• **New Students Marked:** {res['new_students_count']}")
    st.write(f"• **AIS Students:** {res['ais_count']}")
    st.write(f"• **Newcomers:** {res['newcomers_count']}")

    if res["matched_hosts"]:
        st.write(f"• **Assistants/Co-hosts Detected:** {', '.join(res['matched_hosts'])}")
    else:
        st.write("• **Assistants/Co-hosts Detected:** None")

    if res["found_by_name"]:
        with st.expander(f"🔍 {len(res['found_by_name'])} Matched by Name"):
            for item in res["found_by_name"]:
                st.write(f"- {item}")

    if res["unmatched_count"] > 0:
        with st.expander(f"⚠️ {res['unmatched_count']} Unmatched Names"):
            for name in res["unmatched_list"]:
                st.write(f"- {name}")
