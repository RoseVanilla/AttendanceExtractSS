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
    3. Strips any 1-2 character word at the start of a multi-word line (avatar initials, numbers, etc.).
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

        # 3. Strip any 1-2 character prefix followed by a space (e.g. "KQ", "JD", "01", "1.")
        words = line.split()
        if len(words) > 1 and len(words[0]) <= 2:
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
        # 1. VARIABLE-LENGTH ID CHECK (ON RAW UNTOUCHED LINE)
        # ---------------------------------------------------------
        matched_id = None
        raw_compact = raw_trimmed.upper().replace(" ", "")

        for v_id in valid_ids:
            v_id_compact = v_id.upper().replace(" ", "")
            
            # Direct space-insensitive match (e.g. "TL 258" -> "TL258")
            if v_id_compact in raw_compact:
                matched_id = v_id
                break
            
            # O <-> 0 swap fallback
            if 'O' in v_id_compact or '0' in v_id_compact or 'O' in raw_compact or '0' in raw_compact:
                if v_id_compact.replace('O', '0') in raw_compact.replace('O', '0'):
                    matched_id = v_id
                    break

        if matched_id:
            if matched_id not in existing_col_values:
                new_students_count += 1
                existing_col_values.append(matched_id)
                regular_updates.append([matched_id])
            continue  # ID found! Skip cleaning and Steps 2-4 for this line.

        # ---------------------------------------------------------
        # CLEAN THE LINE FOR STEPS 2, 3, & 4
        # ---------------------------------------------------------
        # Strip Zoom tags & 1-2 character avatar prefixes
        part = re.sub(r"^(H|CH|\(Host\)|\(Co-host\))\s+", "", raw_trimmed, flags=re.IGNORECASE)
        words = part.split()
        if len(words) > 1 and len(words[0]) <= 2:
            part = " ".join(words[1:])

        # ---------------------------------------------------------
        # 2. AIS CHECK
        # ---------------------------------------------------------
        matched_ais = next((v for v in AIS_VARIANTS if part.upper().startswith(v) or part.upper().endswith(v)), None)
        if matched_ais:
            ais_count += 1
            clean_name = re.sub(re.escape(matched_ais), "", part, flags=re.IGNORECASE).strip("() ")
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
            
            # Strip numeric prefix if present
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

            # ---------------------------------------------------------
            # RECORD RESULT OR MARK UNMATCHED
            # ---------------------------------------------------------
            if matched_id:
                log_entry = f"{part} ➔ ID: {matched_id}"
                if log_entry not in found_by_name:
                    found_by_name.append(log_entry)

                if matched_id not in existing_col_values:
                    new_students_count += 1
                    existing_col_values.append(matched_id)
                    regular_updates.append([matched_id])
            else:
                # Triggers if 0 candidates match OR if multi-matches couldn't be narrowed to 1 row
                if part and part not in unmatched_participants:
                    unmatched_participants.append(part)


    # STEP G: Write New Rows to Selected Column
    current_row = start_write_row

    # 1. Write Regular IDs
    if regular_updates:
        end_row = current_row + len(regular_updates) - 1
        sheets.values().update(
            spreadsheetId=SHEET_ID,
            range=f"'{target_sheet_name}'!{target_col_letter}{current_row}:{target_col_letter}{end_row}",
            valueInputOption="USER_ENTERED",
            body={"values": regular_updates}
        ).execute()
        current_row = end_row + 3  # Leave 2 blank rows

    # 2. Write AIS Students
    if ais_updates:
        end_row = current_row + len(ais_updates) - 1
        sheets.values().update(
            spreadsheetId=SHEET_ID,
            range=f"'{target_sheet_name}'!{target_col_letter}{current_row}:{target_col_letter}{end_row}",
            valueInputOption="USER_ENTERED",
            body={"values": ais_updates}
        ).execute()
        current_row = end_row + 3  # Leave 2 blank rows

    # 3. Write Newcomers
    if newcomer_updates:
        end_row = current_row + len(newcomer_updates) - 1
        sheets.values().update(
            spreadsheetId=SHEET_ID,
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

col_input = st.text_input("Target Column Letter (e.g., B, C, D, AA):", value="").strip().upper()

uploaded_images = st.file_uploader(
    "Upload Zoom Screenshot(s)", 
    type=["jpg", "jpeg", "png"], 
    accept_multiple_files=True
)

if st.button("Process Attendance"):
    num_screenshots = len(uploaded_images)
    with st.spinner(f"Processing {num_screenshots} screenshot(s) with OCR & logging to Column {col_input}..."):
        res = process_zoom_ocr_attendance(uploaded_images, col_input)
            
        st.success(f"Attendance Logged in Column {res['target_column']}!")
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
