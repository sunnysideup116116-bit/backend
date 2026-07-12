import PyPDF2

with open('Ai support system.pdf', 'rb') as file:
    reader = PyPDF2.PdfReader(file)
    text = ""
    for page in reader.pages:
        text += page.extract_text() + "\n"
        
with open('pdf_text.txt', 'w', encoding='utf-8') as out_file:
    out_file.write(text)
print("PDF text extracted to pdf_text.txt")
