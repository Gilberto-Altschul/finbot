import pdfplumber

caminho_pdf = "C:/Users/gilbe/Downloads/Fatura_01-06-2026.pdf"  # ajuste o caminho do seu arquivo
senha = "941959"  # os dígitos do CPF que você usa

with pdfplumber.open(caminho_pdf, password=senha) as pdf:
    for i, page in enumerate(pdf.pages):
        text = page.extract_text(layout=True)
        print(f"\n=== PÁGINA {i+1} ===")
        print(text if text else "(sem texto extraível)")