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
    "AIS", "AIS)", "WITH MR BOULES", "IN CLASS)", 
    "WITH MR BOULES)", "IN CLASS", "AIS STUDENT)", "AIS STUDENT"
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
            first_initial = words[1][0].upper() if len(words) > 1 else ""
            second_initial = words[2][0].upper() if len(words) > 2 else ""

            is_single = len(candidate_initials) == 1 and candidate_initials == first_initial
            is_double = (
                len(candidate_initials) == 2 
                and second_initial 
                and candidate_initials == (first_initial + second_initial)
            )

            if is_single or is_double:
                words.pop(0)
                line = " ".join(words)

        cleaned_lines.append(line)

    return cleaned_lines

def get_sheets_service():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=scopes)
    return build("sheets", "v4", credentials=creds)

def find_first_empty_column(sheets, spreadsheet_id, sheet_name, start_col=2, check_row=7):
    """
    Scans columns starting from Column B (start_col=2) to find the first 
    column where Row 7 is empty.
    """
    res = sheets.values().get(
        spreadsheetId=spreadsheet_id,
        range=f"'{sheet_name}'!A1:ZZ100"
    ).execute()
    rows = res.get("values", [])
    
    row_idx = check_row - 1  # 0-based index for Row 7
    
    col = start_col
    while True:
        if row_idx < len(rows):
            row = rows[row_idx]
            if col - 1 < len(row):
                cell_val = row[col - 1].strip()
                if cell_val:  # Column has data at Row 7, move to next column
                    col += 1
                    continue
        break  # Found empty column!
        
    return col

# ==========================================
# MAIN OCR PROCESSING ENGINE
# ==========================================
def process_zoom_ocr_attendance(uploaded_files):
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

    # STEP C: Dynamically Find First Empty Column (Row 7) starting at Column B (2)
    target_col_idx = find_first_empty_column(sheets, SHEET_ID, target_sheet_name, start_col=2, check_row=7)
    target_col_letter = column_to_letter(target_col_idx)

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
                if h != "BOULES Ramzy":
                    if h == "Batool Khaled":
                        ap = "Batoul "
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
    existing_col_values = []
    updates_to_append = []

    for part in cleaned_lines:
        is_host = any(h.upper() in part.upper() for h in HOSTS)

        # 1. Variable-Length ID Check
        matched_id = None
        for v_id in valid_ids:
            if v_id in part.upper():
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
            clean_name = re.sub(re.escape(matched_ais), "", part, flags=re.IGNORECASE).strip().replace("(", "").replace(")", "")
            formatted = f"AIS {clean_name}"
            if formatted not in existing_col_values:
                new_students_count += 1
                existing_col_values.append(formatted)
                updates_to_append.append([formatted])
            continue

        # 3. Newcomer Check
        if part.upper().endswith("NEWCOMER"):
            newcomers_count += 1
            if part not in existing_col_values:
                new_students_count += 1
                existing_col_values.append(part)
                updates_to_append.append([part])
            continue

        # 4. Tokenized Name Fallback Matching
        if not is_host:
            words = [w for w in part.split() if len(w) > 0]
            if words:
                w1 = words[0].upper()
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
                    if matched_id not in existing_col_values:
                        new_students_count += 1
                        existing_col_values.append(matched_id)
                        updates_to_append.append([matched_id])
                else:
                    if part not in unmatched_participants:
                        unmatched_participants.append(part)

    # STEP G: Write New Rows to Attendance Sheet starting at Row 7
    if updates_to_append:
        start_range = f"'{target_sheet_name}'!{target_col_letter}7"
        sheets.values().update(
            spreadsheetId=SHEET_ID,
            range=start_range,
            valueInputOption="USER_ENTERED",
            body={"values": updates_to_append}
        ).execute()

    return {
        "target_column": target_col_letter,
        "new_students_count": new_students_count,
        "ais_count": ais_count,
        "newcomers_count": newcomers_count,
        "unmatched_count": len(unmatched_participants),
        "unmatched_list": unmatched_participants
    }

# ==========================================
# STREAMLIT USER INTERFACE
# ==========================================
st.title("Zoom Attendance OCR Processor")

uploaded_images = st.file_uploader(
    "Upload Zoom Screenshot(s)", 
    type=["jpg", "jpeg", "png"], 
    accept_multiple_files=True
)

if st.button("Process Attendance"):
    if not uploaded_images:
        st.warning("Please upload at least one image.")
    else:
        with st.spinner("Scanning sheet for next empty column and running OCR..."):
            res = process_zoom_ocr_attendance(uploaded_images)
            st.success(f"Attendance Logged in Column {res['target_column']} (Row 7 onwards)!")
            st.write(f"• **New Students Marked:** {res['new_students_count']}")
            st.write(f"• **AIS Students:** {res['ais_count']}")
            st.write(f"• **Newcomers:** {res['newcomers_count']}")

            if res["unmatched_count"] > 0:
                with st.expander(f"⚠️ {res['unmatched_count']} Unmatched Names"):
                    for name in res["unmatched_list"]:
                        st.write(f"- {name}")
        st.error("Please enter a column letter.")
        st.stop()

    # --- Step 1: Connect to Google Sheets ---
    try:
        with st.spinner("Connecting to Google Sheets..."):
            secret_data = st.secrets["gcp_service_account"]
            
            # Handle secrets whether stored as a string or a TOML dictionary
            if isinstance(secret_data, str):
                service_account_info = json.loads(secret_data)
            else:
                service_account_info = dict(secret_data)

            credentials = Credentials.from_service_account_info(
                service_account_info,
                scopes=[
                    "https://www.googleapis.com/auth/spreadsheets",
                    "https://www.googleapis.com/auth/drive"
                ]
            )
            gc = gspread.authorize(credentials)

            # Open sheets
            sh = gc.open('AS STUDENTS LIST NOV 2026')
            ws = sh.get_worksheet(0)
            all_log_rows = ws.get_all_values()

            log_header = [str(h).strip().lower() for h in all_log_rows[0]] if all_log_rows else []
            id_col_idx = log_header.index("id") if "id" in log_header else 0
            valid = [str(row[id_col_idx]).strip() for row in all_log_rows[1:] if len(row) > id_col_idx and row[id_col_idx]]

            att = gc.open('ATTENDANCE - AL Nov 26')
            att_sheets = att.worksheets()
            attws = att_sheets[1] if len(att_sheets) > 1 else att_sheets[0]

            col_idx = col_to_index(col_input)
            existing_col_data = attws.col_values(col_idx)
            preatt = [str(cell) for cell in existing_col_data if cell]
            nextrow = max(7, len(existing_col_data) + 1)

    except Exception as e:
        st.error(f"Failed to connect to Google Sheets: {e}")
        st.stop()

    # --- Step 2: Batch OCR Processing Across All Images ---
    Prtc = []
    try:
        with st.spinner(f"Analyzing {len(uploaded_files)} screenshot(s) with OCR Space API..."):
            for img in uploaded_files:
                file_bytes = img.getvalue()
                response = requests.post(
                    "https://api.ocr.space/parse/image",
                    files={"file": (img.name, file_bytes, img.type)},
                    data={"apikey": API_KEY, "language": "eng", "ocrengine": "2", "isTable": True}
                )

                result = response.json()
                if "ParsedResults" in result and result["ParsedResults"]:
                    raw_text = result["ParsedResults"][0]["ParsedText"]
                    Prtc.extend([line.strip() for line in raw_text.splitlines() if line.strip()])

            if not Prtc:
                st.error("No text/participants found in the uploaded screenshot(s).")
                st.stop()

            st.success(f"Successfully extracted {len(Prtc)} total lines across {len(uploaded_files)} image(s).")

    except Exception as e:
        st.error(f"OCR Processing failed: {e}")
        st.stop()

    # --- Step 3: Host Detection & Header Updates ---
    try:
        with st.spinner("Processing participants and updating attendance..."):
            host = [
                "BOULES Ramzy", "Farida Fayez", "Farah Ashraf", "Judy Hassanien", 
                "Parthinia Mazouz", "Nada Wael", "Judi Ziad", "Rivana Aly", 
                "Menna Amr", "Batool Khaled", "Jaidaa Gomaa", "Lobna Mohamed"
            ]

            matched_hosts = []
            for p in Prtc:
                matched_h = next((h for h in host if p.upper().startswith(h.upper())), None)
                if matched_h:
                    ap = matched_h
                    if matched_h != "BOULES Ramzy":
                        if matched_h == "Batool Khaled":
                            ap = "Batoul "
                        elif " " in ap:
                            space = ap.index(" ")
                            ap = ap[:space]
                    if ap not in matched_hosts:
                        matched_hosts.append(ap)

            if matched_hosts:
                final_text = " & ".join(matched_hosts)
                attws.update_cell(4, col_idx, final_text)
                st.info(f"Updated row 4 header with assistants: **{final_text}**")
            else:
                st.warning("No hosts/co-hosts found in screenshot(s).")

            # --- Step 4: Participant ID Extraction & Category Matching ---
            counta, countf, present, skl, newcomers = 0, 0, 0, 0, 0
            idf, idnf = [], []
            id_updates, ais_updates, newcomer_updates = [], [], []
            start_write_row = nextrow

            for part in Prtc:
                is_host = any(part.upper().startswith(h.upper()) for h in host)
                
                found = False
                for ext in range(len(part) - 4):
                    check = part[ext : ext + 5].upper()
                    if check in valid:
                        found = True
                        idf.append(check)
                        countf += 1
                        if check not in preatt:
                            counta += 1
                            preatt.append(check)
                            id_updates.append([check])
                            nextrow += 1
                        present += 1
                        break

                vsid = ["AIS", "AIS)", "WITH MR BOULES", "IN CLASS)", "WITH MR BOULES)", "IN CLASS", "AIS STUDENT)", "AIS STUDENT"]
                matched_sid = next((sid for sid in vsid if part.upper().startswith(sid) or part.upper().endswith(sid)), None)

                if matched_sid:
                    skl += 1
                    clean_name = re.sub(re.escape(matched_sid), "", part, flags=re.IGNORECASE).strip("() ")
                    formatted_name = f"AIS {clean_name}"
                    if formatted_name not in preatt:
                        counta += 1
                        preatt.append(formatted_name)
                        ais_updates.append([formatted_name])
                        nextrow += 1
                elif part.upper().endswith("NEWCOMER"):
                    newcomers += 1
                    if part not in preatt:
                        counta += 1
                        preatt.append(part)
                        newcomer_updates.append([part])
                        nextrow += 1
                elif not found and not is_host:
                    if part not in idnf:
                        idnf.append(part)

            # --- Step 5: Write Updates to Google Sheets ---
            batch_updates = id_updates + ais_updates + newcomer_updates
            if batch_updates:
                start_cell = rowcol_to_a1(start_write_row, col_idx)
                attws.update(start_cell, batch_updates)

        # --- Step 6: Display Results UI ---
        st.success("Attendance successfully processed from OCR screenshots!")

        st.subheader("Run Summary")
        m1, m2, m3 = st.columns(3)
        m1.metric("New Students Added", counta)
        m2.metric("AIS School Students", skl)
        m3.metric("Newcomers", newcomers)

        reps = sum(1 for crep in set(idf) if idf.count(crep) > 1)
        if reps > 0:
            st.warning(f"⚠️ {reps} IDs were repeated across uploaded screenshots.")

        if idnf:
            with st.expander(f"No Valid ID Found for {len(idnf)} Entries"):
                for name in idnf:
                    st.write(f"• {name}")

    except Exception as e:
        st.error(f"An error occurred during execution: {e}")
    # --- Step 1: Connect to Google Sheets ---
    try:
        with st.spinner("Connecting to Google Sheets..."):
            credentials = Credentials.from_service_account_info(
                st.secrets["gcp_service_account"],
                scopes=[
                    "https://www.googleapis.com/auth/spreadsheets",
                    "https://www.googleapis.com/auth/drive"
                ]
            )
            gc = gspread.authorize(credentials)

            # Open student list and attendance sheet
            sh = gc.open('AS STUDENTS LIST NOV 2026')
            ws = sh.get_worksheet(0)
            all_log_rows = ws.get_all_values()

            log_header = [str(h).strip().lower() for h in all_log_rows[0]] if all_log_rows else []
            id_col_idx = log_header.index("id") if "id" in log_header else 0
            valid = [str(row[id_col_idx]).strip() for row in all_log_rows[1:] if len(row) > id_col_idx and row[id_col_idx]]

            att = gc.open('ATTENDANCE - AL Nov 26')
            att_sheets = att.worksheets()
            attws = att_sheets[1] if len(att_sheets) > 1 else att_sheets[0]

            col_idx = col_to_index(col_input)
            existing_col_data = attws.col_values(col_idx)
            preatt = [str(cell) for cell in existing_col_data if cell]
            nextrow = max(7, len(existing_col_data) + 1)

    except Exception as e:
        st.error(f"Failed to connect to Google Sheets: {e}")
        st.stop()

    # --- Step 2: OCR Processing ---
    try:
        with st.spinner("Analyzing screenshot with OCR Space API..."):
            file_bytes = uploaded_file.getvalue()
            response = requests.post(
                "https://api.ocr.space/parse/image",
                files={"file": (uploaded_file.name, file_bytes, uploaded_file.type)},
                data={"apikey": API_KEY, "language": "eng", "ocrengine": "2", "isTable": True}
            )

            result = response.json()
            Prtc = []

            if "ParsedResults" in result and result["ParsedResults"]:
                raw_text = result["ParsedResults"][0]["ParsedText"]
                Prtc = [line.strip() for line in raw_text.splitlines() if line.strip()]

            if not Prtc:
                st.error("No text/participants found in the uploaded screenshot.")
                st.stop()

            st.success(f"Successfully extracted {len(Prtc)} lines from screenshot.")

    except Exception as e:
        st.error(f"OCR Processing failed: {e}")
        st.stop()

    # --- Step 3: Host Detection & Header Updates ---
    try:
        with st.spinner("Processing participants and updating attendance..."):
            host = [
                "BOULES Ramzy", "Farida Fayez", "Farah Ashraf", "Judy Hassanien", 
                "Parthinia Mazouz", "Nada Wael", "Judi Ziad", "Rivana Aly", 
                "Menna Amr", "Batool Khaled", "Jaidaa Gomaa", "Lobna Mohamed"
            ]

            matched_hosts = []
            for p in Prtc:
                # Find matching host using .startswith() logic
                matched_h = next((h for h in host if p.upper().startswith(h.upper())), None)
                if matched_h:
                    ap = matched_h
                    if matched_h != "BOULES Ramzy":
                        if matched_h == "Batool Khaled":
                            ap = "Batoul "
                        elif " " in ap:
                            space = ap.index(" ")
                            ap = ap[:space]
                    matched_hosts.append(ap)

            if matched_hosts:
                final_text = " & ".join(matched_hosts)
                attws.update_cell(4, col_idx, final_text)
                st.info(f"Updated row 4 header with assistants: **{final_text}**")
            else:
                st.warning("No hosts/co-hosts found in screenshot.")

            # --- Step 4: Participant ID Extraction & Category Matching ---
            counta, countf, present, skl, newcomers = 0, 0, 0, 0, 0
            idf, idnf = [], []
            id_updates, ais_updates, newcomer_updates = [], [], []
            start_write_row = nextrow

            for part in Prtc:
                # Check if line is a host line via .startswith()
                is_host = any(part.upper().startswith(h.upper()) for h in host)
                
                found = False
                for ext in range(len(part) - 4):
                    check = part[ext : ext + 5].upper()
                    if check in valid:
                        found = True
                        idf.append(check)
                        countf += 1
                        if check not in preatt:
                            counta += 1
                            preatt.append(check)
                            id_updates.append([check])
                            nextrow += 1
                        present += 1
                        break

                vsid = ["AIS", "AIS)", "WITH MR BOULES", "IN CLASS)", "WITH MR BOULES)", "IN CLASS", "AIS STUDENT)", "AIS STUDENT"]
                matched_sid = next((sid for sid in vsid if part.upper().startswith(sid) or part.upper().endswith(sid)), None)

                if matched_sid:
                    skl += 1
                    clean_name = re.sub(re.escape(matched_sid), "", part, flags=re.IGNORECASE).strip("() ")
                    formatted_name = f"AIS {clean_name}"
                    if formatted_name not in preatt:
                        counta += 1
                        preatt.append(formatted_name)
                        ais_updates.append([formatted_name])
                        nextrow += 1
                elif part.upper().endswith("NEWCOMER"):
                    newcomers += 1
                    if part not in preatt:
                        counta += 1
                        preatt.append(part)
                        newcomer_updates.append([part])
                        nextrow += 1
                elif not found and not is_host:
                    idnf.append(part)

            # --- Step 5: Write Updates to Google Sheets ---
            batch_updates = id_updates + ais_updates + newcomer_updates
            if batch_updates:
                start_cell = rowcol_to_a1(start_write_row, col_idx)
                attws.update(start_cell, batch_updates)

        # --- Step 6: Display Results UI ---
        st.success("Attendance successfully processed from OCR screenshot!")

        st.subheader("Run Summary")
        m1, m2, m3 = st.columns(3)
        m1.metric("New Students Added", counta)
        m2.metric("AIS School Students", skl)
        m3.metric("Newcomers", newcomers)

        # Repeated IDs warning
        reps = sum(1 for crep in set(idf) if idf.count(crep) > 1)
        if reps > 0:
            st.warning(f"⚠️ {reps} IDs were repeated in the uploaded screenshot.")

        # Display unrecognized participants
        if idnf:
            with st.expander(f"No Valid ID Found for {len(idnf)} Entries"):
                for name in idnf:
                    st.write(f"• {name}")

    except Exception as e:
        st.error(f"An error occurred during execution: {e}")
      
