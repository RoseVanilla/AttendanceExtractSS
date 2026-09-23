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
def get_sheets_service():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    secret_data = st.secrets["gcp_service_account"]
    creds = Credentials.from_service_account_info(dict(secret_data), scopes=scopes)
    return build("sheets", "v4", credentials=creds)

SHEET_ID = "1taKxfTBnASlDI3dYJEYzFPppkjUr3-zxBgEnlpaigeU"           # Tab 2 / Index 1 attendance sheet
STUDENT_LIST_SHEET_ID = "17ulfV3Ecp-UlHXLOZT4bfKovy7B5FCPZ3efUkdZnM-w"  # Roster Sheet (Col A=ID, Col B=Name)
OCR_SPACE_API_KEY = "K83980812088957"

HOSTS = [
    "BOULES Ramzy", "Farida Fayez", "Farah Ashraf", "Judy Hassanien",
    "Parthinia Mazouz", "Nada Wael", "Judi Ziad", "Rivana Aly",
    "Menna Amr", "Batool Khaled", "Jaidaa Gomaa", "Lobna Mohamed"
]

AIS_VARIANTS = [
    "AIS", "AIS)", "WITH MR BOULES", "WITH MR BOULES IN CLASS)", 
    "WITH MR BOULES)", "WITH MR BOULES IN CLASS", "AIS STUDENT)", "AIS STUDENT"
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
    """
    1. Removes standalone avatar initials (1-2 letters).
    2. Strips Zoom host/co-host prefixes.
    3. Strips prepended avatar initials attached to names (e.g., 'JD John Doe').
    """
    cleaned_lines = []

    for line in raw_lines:
        line = line.strip()
        if not line:
            continue

        # 1. Skip standalone avatar initials (1 or 2 letters only)
        if re.match(r"^[A-Za-z]{1,2}$", line):
            continue

        # 2. Strip Zoom host/co-host tags if prepended
        line = re.sub(r"^(H|CH|\(Host\)|\(Co-host\))\s+", "", line, flags=re.IGNORECASE)

        # 3. Strip prepended avatar initials attached to names (e.g., "JD John Doe")
        words = line.split()
        if len(words) > 1 and re.match(r"^[A-Za-z]{1,2}$", words[0]):
                        candidate_initials = words[0].upper()
            first_initial = words[1][0].upper() if len(words[1]) > 0 else ""
            second_initial = words[2][0].upper() if len(words) > 2 and len(words[2]) > 0 else ""

            is_single = len(candidate_initials) == 1 and candidate_initials == first_initial
            is_double_full = (
                len(candidate_initials) == 2
                and second_initial
                and candidate_initials == (first_initial + second_initial)
            )
            is_double_single_name = (
                len(candidate_initials) == 2
                and len(words) == 2
                and candidate_initials.startswith(first_initial)
            )

            if is_single or is_double_full or is_double_single_name:
                words = words[1:]
                line = " ".join(words)


        cleaned_lines.append(line)

    return cleaned_lines

def get_sheets_service():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    secret_data = st.secrets["gcp_service_account"]

    # Handle string (raw JSON) vs TOML dictionary formats
    if isinstance(secret_data, str):
        info = json.loads(secret_data)
    else:
        info = dict(secret_data)

    # Convert escaped newline characters in private_key if needed
    if "private_key" in info and "\\n" in info["private_key"]:
        info["private_key"] = info["private_key"].replace("\\n", "\n")

    creds = Credentials.from_service_account_info(info, scopes=scopes)
    return build("sheets", "v4", credentials=creds)


def get_column_write_info(sheets, spreadsheet_id, sheet_name, col_letter):
    """
    Reads the designated column to find existing entries (Row 7+)
    and calculates the next available empty row index.
    """
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
def process_zoom_ocr_attendance(uploaded_files, target_col_letter):
    service = get_sheets_service()
    sheets = service.spreadsheets()

    # STEP A: Read Roster from STUDENT_LIST_SHEET_ID
    roster_res = sheets.values().get(
        spreadsheetId=STUDENT_LIST_SHEET_ID,
        range="A1:B1000"
    ).execute()

    master_rows = roster_res.get("values", [])
    registered_students = []
    if len(master_rows) > 1:
        for row in master_rows[1:]:
            s_id = row[0].strip().upper() if len(row) > 0 and row[0] else ""
            s_name = row[1].strip() if len(row) > 1 and row[1] else ""
            if s_id:
                registered_students.append({"id": s_id, "name": s_name})

    valid_ids = [s["id"] for s in registered_students]

    # STEP B: Target Tab 2 (Index 1) in SHEET_ID
    meta = sheets.get(spreadsheetId=SHEET_ID).execute()
    sheet_list = meta.get("sheets", [])
    target_sheet_name = sheet_list[1]["properties"]["title"] if len(sheet_list) > 1 else sheet_list[0]["properties"]["title"]

   # STEP C: Inspect Selected Column for Append Position & Existing Entries
    existing_col_values, start_write_row = get_column_write_info(
        sheets, SHEET_ID, target_sheet_name, target_col_letter
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

    # Clean raw OCR lines (Removes profile initials & Host tags)
    cleaned_lines = clean_ocr_lines(raw_lines)

    # STEP E: Host Extraction & Row 4 Header Update
    matched_hosts = []
        for line in cleaned_lines:
        for h in HOSTS:
            if h.upper() in line.upper():
                ap = h
                if "BOULES" not in h.upper():
                    if h == "Batool Khaled":
                        ap = "Batool "
                    elif " " in ap:
                        ap = ap.split(" ")[0]
                    if ap not in matched_hosts:
                        matched_hosts.append(ap)


    if matched_hosts:
        host_header_text = " & ".join(matched_hosts)
        sheets.values().update(
            spreadsheetId=SHEET_ID,
            range=f"'{target_sheet_name}'!{target_col_letter}4",
            valueInputOption="USER_ENTERED",
            body={"values": [[host_header_text]]}
        ).execute()

# STEP F: Participant Matching Logic
    new_students_count = 0
    ais_count = 0
    newcomers_count = 0
    unmatched_participants = []
    found_by_name = []
    updates_to_append = []

    for part in cleaned_lines:
        is_host = any(h.upper() in part.upper() for h in HOSTS)

        # 1. Variable-Length ID Check (with O <-> 0 fallback swap)
        matched_id = None
        part_upper = part.upper()
        for v_id in valid_ids:
            v_id_upper = v_id.upper()
            
            if v_id_upper in part_upper:
                matched_id = v_id
                break
            
            if 'O' in v_id_upper or '0' in v_id_upper or 'O' in part_upper or '0' in part_upper:
                v_id_normalized = v_id_upper.replace('O', '0')
                part_normalized = part_upper.replace('O', '0')
                if v_id_normalized in part_normalized:
                    matched_id = v_id
                    break

        if matched_id:
            if matched_id not in existing_col_values:
                new_students_count += 1
                existing_col_values.append(matched_id)
                updates_to_append.append([matched_id])
            continue

        # 2. AIS Check
        matched_ais = next((v for v in AIS_VARIANTS if part.upper().startswith(v) or part.upper().endswith(v)), None)
        if matched_ais:
            ais_count += 1
            clean_name = re.sub(re.escape(matched_ais), "", part, flags=re.IGNORECASE).strip("() ")
            formatted = f"AIS {clean_name}"
            if formatted not in existing_col_values:
                new_students_count += 1
                existing_col_values.append(formatted)
                updates_to_append.append([formatted])
            continue

        # 3. Newcomer Check
        if part.upper().endswith("NEWCOMER" or "NEW COMER"):
            newcomers_count += 1
            if part not in existing_col_values:
                new_students_count += 1
                existing_col_values.append(part)
                updates_to_append.append([part])
            continue

        # 4. Tokenized Name Fallback Matching
        if not is_host:
            words = [w for w in part.split() if len(w) > 0]
            
            if words and any(char.isdigit() for char in words[0]):
                words.pop(0)

            if words:
                w1 = words[0].upper()
                        # Single-name fallback (e.g., participant only typed "Karen")
        if len(words) == 1:
            single_matches = [
                s for s in registered_students 
                if s['name'].strip().upper().split()[0] == w1
            ]
            if len(single_matches) == 1:
                matched_id = single_matches[0]['id']
                candidate_rom = single_matches[0]['name']

                candidate_rows = [s for s in registered_students if w1 in s["name"].upper()]

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
                        updates_to_append.append([matched_id])
                else:
                    if part not in unmatched_participants:
                        unmatched_participants.append(part)

    # STEP G: Write New Rows to Selected Column
    if updates_to_append:
        start_range = f"'{target_sheet_name}'!{target_col_letter}{start_write_row}"
        sheets.values().update(
            spreadsheetId=SHEET_ID,
            range=start_range,
            valueInputOption="USER_ENTERED",
            body={"values": updates_to_append}
        ).execute()

    return {
        "target_column": target_col_letter,
        "start_row": start_write_row,
        "new_students_count": new_students_count,
        "ais_count": ais_count,
        "newcomers_count": newcomers_count,
        "found_by_name": found_by_name,          
        "unmatched_count": len(unmatched_participants),
        "unmatched_list": unmatched_participants
    }

# ==========================================
# STREAMLIT USER INTERFACE
# ==========================================
st.set_page_config(page_title="Zoom Attendance OCR", page_icon="📋")
st.title("Zoom Attendance OCR Processor")

col_input = st.text_input("Target Column Letter (e.g., B, C, D, AA):", value="").strip().upper()

uploaded_images = st.file_uploader(
    "Upload Zoom Screenshot(s)", 
    type=["jpg", "jpeg", "png"], 
    accept_multiple_files=True
)

if st.button("Process Attendance"):
    if not col_input or not re.match(r"^[A-Z]{1,3}$", col_input):
        st.error("Please enter a valid column letter (e.g., B, C, AA).")
    elif not uploaded_images:
        st.warning("Please upload at least one screenshot.")
    else:
        with st.spinner(f"Processing OCR & logging to Column {col_input}..."):
            res = process_zoom_ocr_attendance(uploaded_images, col_input)
            
            st.success(f"Attendance Logged in Column {res['target_column']} (Starting at Row {res['start_row']})!")
            st.write(f"• **New Students Marked:** {res['new_students_count']}")
            st.write(f"• **AIS Students:** {res['ais_count']}")
            st.write(f"• **Newcomers:** {res['newcomers_count']}")

            if res["found_by_name"]:
                with st.expander(f"🔍 {len(res['found_by_name'])} Matched by Name (Fallback)"):
                    for item in res["found_by_name"]:
                        st.write(f"- {item}")

            if res["unmatched_count"] > 0:
                with st.expander(f"⚠️ {res['unmatched_count']} Unmatched Names"):
                    for name in res["unmatched_list"]:
                        st.write(f"- {name}")
