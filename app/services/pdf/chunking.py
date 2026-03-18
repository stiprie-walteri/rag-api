import fitz
import re

def get_fallback_toc(doc: fitz.Document) -> list:
    """Generates a pseudo-TOC by scanning the first 10 pages for internal links."""
    all_dests = []
    # Scan first 10 pages for internal links (TOC is usually at the start)
    scan_pages = min(10, len(doc))
    for p_num in range(scan_pages):
        page = doc.load_page(p_num)
        for link in page.get_links():
            if link["kind"] == fitz.LINK_GOTO:
                rect = link["from"]
                text = page.get_textbox(rect).strip()
                text = text.replace('\n', ' ')
                text = re.sub(r'\.{2,}\s*\d*$', '', text).strip()
                
                if not text:
                    text = f"Section starting on Page {link['page'] + 1}"
                    level = 1
                else:
                    match = re.match(r'^([\d\.]+)', text)
                    if match:
                        numbering = match.group(1).strip('.')
                        level = len(numbering.split('.')) if numbering else 1
                    else:
                        level = 1
                
                page_idx = link["page"]
                # We store the entire link object to extract its "to" Point
                all_dests.append({"page": page_idx, "level": level, "title": text, "link": link})
    
    if not all_dests:
        raise ValueError("The provided PDF does not contain a Table of Contents (TOC) and no internal links were found to chunk by.")
        
    # Sort by target page, then by y-coordinate of the link destination (top to bottom)
    def sort_key(d):
        point = d["link"].get("to")
        y_val = point.y if point else 0.0
        return (d["page"], y_val)
        
    all_dests.sort(key=sort_key)
    
    toc = []
    for dest in all_dests:
        # Link pages in PyMuPDF are 0-indexed, TOC uses 1-indexed for page
        # We append the link dict as the 4th element to mimic simple=False TOC result
        toc.append([dest["level"], dest["title"], dest["page"] + 1, {"to": dest["link"].get("to")}])
        
    return toc

def extract_chunks_from_toc(doc: fitz.Document, toc: list) -> list:
    """Iterates through the TOC and extracts text blocks belonging to each section."""
    num_pages = len(doc)
    chunks = []
    
    for i in range(len(toc)):
        entry = toc[i]
        lvl = entry[0]
        title = entry[1]
        start_page = entry[2] - 1  # 0-indexed
        
        dest_dict = entry[3] if len(entry) > 3 else {}
        start_point = dest_dict.get("to") if isinstance(dest_dict, dict) else None
        start_y = start_point.y if start_point else None
        
        # Determine end page and end y based on the next TOC entry
        if i + 1 < len(toc):
            next_entry = toc[i+1]
            end_page = next_entry[2] - 1
            next_dest = next_entry[3] if len(next_entry) > 3 else {}
            next_point = next_dest.get("to") if isinstance(next_dest, dict) else None
            end_y = next_point.y if next_point else None
        else:
            end_page = num_pages - 1
            end_y = None
            
        # In case of out-of-order TOC entries
        if end_page < start_page:
            end_page = start_page
            end_y = None
            
        text_parts = []
        for p_num in range(start_page, end_page + 1):
            if 0 <= p_num < num_pages:
                page_obj = doc.load_page(p_num)
                blocks = page_obj.get_text("blocks")
                # Sort blocks vertically (y0), then horizontally (x0)
                blocks.sort(key=lambda b: (b[1], b[0]))
                
                for b in blocks:
                    if len(b) >= 7 and b[6] == 0:  # Text block
                        x0, y0, x1, y1, block_text, block_no, block_type = b
                        
                        # Apply starting boundary if on the start page
                        if p_num == start_page and start_y is not None:
                            # If building is entirely above the start Y coordinate (with a 5px margin), skip
                            if y1 < start_y - 5:
                                continue
                                
                        # Apply ending boundary if on the end page
                        if p_num == end_page and end_y is not None:
                            # If block's bottom reaches or exceeds the end section's Y coordinate, it belongs to the NEXT section
                            if y1 > end_y - 5:
                                continue
                                
                        text_parts.append(block_text.strip())
                
        chunk_text = "\n\n".join(text_parts).strip()
        
        chunks.append({
            "level": lvl,
            "title": title,
            "start_page": start_page + 1,
            "end_page": end_page + 1,
            "text": chunk_text
        })
        
    return chunks

def chunk_pdf(pdf_bytes: bytes) -> list:
    """Takes PDF bytes and returns extracted section chunks using TOC/Internal Links."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    
    # We need simple=False to get the destination properties (the exact Point with x and y coordinates)
    toc = doc.get_toc(simple=False)
    
    # Fallback to internal links if no TOC
    if not toc:
        toc = get_fallback_toc(doc)
            
    chunks = extract_chunks_from_toc(doc, toc)
    return chunks
