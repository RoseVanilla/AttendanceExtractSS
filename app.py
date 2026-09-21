import streamlit as st
import gspread
import requests
import re
import json
from gspread.utils import rowcol_to_a1
from google.oauth2.service_account import Credentials

st.set_page_config(page_title="OCR Attendance Tracker", page_icon="📷")

st.title("📋 Automated Attendance Tracker")
st.write("Upload Zoom screenshots to extract participant attendance using OCR and update Google Sheets.")

# --- File & Input Controls ---
uploaded_files = st.file_uploader(
    "Select Zoom Screenshots", 
    type=["png", "jpg", "jpeg"], 
    accept_multiple_files=True
)
col_input = st.text_input("Enter target column letter (e.g., A, B, C):").upper()

API_KEY = "K83980812088957"

def col_to_index(col_str):
    if col_str.isdigit():
        return int(col_str)
    num = 0
    for char in col_str:
        num = num * 26 + (ord(char) - ord('A') + 1)
    return num

if st.button("Process Attendance from Images"):
    if not uploaded_files:
        st.error("Please upload at least one screenshot.")
        st.stop()
    if not col_input:
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
                matched_h = next((h for h in host if p[range(3)::].upper().startswith(h.upper())))
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

                is_host = any(part[range(3)::].upper().startswith(h.upper()) for h in host)
                
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

                vsid = ["AIS", "AIS)", "WITH MR BOULES", "WITH MR BOULES IN CLASS)", "WITH MR BOULES)", "IN CLASS", "AIS STUDENT)", "AIS STUDENT"]
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
                elif part.upper().endswith("NEWCOMER" or "NEW COMER"):
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
      
