"""
Diagnóstico (só leitura) de uma fatura em PDF: qual parser leu, o que ele extraiu de "estorno" e qual vencimento foi achado.

    python diag_pdf.py "C:\\caminho\\fatura.pdf" [senha]

Rode na raiz do projeto, com o .venv ativo. Não grava nada, não chama Gemini e não imprime nomes nem números de cartão.
"""
import io
import re
import sys

import pdfplumber


def main(caminho: str, senha: str | None) -> None:
    import app.pdf_import as p
    from app.parsers import detectar_banco_e_tipo
    bruto = open(caminho, "rb").read()
    with pdfplumber.open(io.BytesIO(bruto), password=senha) as pdf:
        texto = "\n\n".join(pg.extract_text(layout=True) or "" for pg in pdf.pages)
    print(f"texto extraído: {len(texto)} caracteres | tipo do documento: {p._tipo_documento(texto)}")
    print(f"banco/tipo detectado: {detectar_banco_e_tipo(texto)}")
    print(f"dia de vencimento lido pelo app: {p.dia_vencimento_do_pdf(texto)}")
    print("\nlinhas com 'vencimento' (valores mascarados):")
    for ln in texto.splitlines():
        if re.search(r"vencimento", ln, re.I):
            print("   ", re.sub(r"\s{2,}", " | ", ln.strip())[:140])
    print("\nlinhas com 'estorno' ou 'anuidade':")
    for ln in texto.splitlines():
        if re.search(r"estorno|anuidade", ln, re.I):
            print("   ", re.sub(r"\s{2,}", " | ", ln.strip())[:140])
    try:
        from app.parsers import itau_credito as it
        print(f"\nparser do Itaú: eh_fatura={it.eh_fatura(texto)} | vencimento lido={it.vencimento(texto)}")
        ri = it.parse(texto)
        if ri:
            soma = sum(t["amount"] if t["type"] == "expense" else -t["amount"] for t in ri["transactions"])
            print(f"   {len(ri['transactions'])} lançamentos, soma líquida R$ {soma:.2f}")
            for t in ri["transactions"]:
                print("   ", {k: t.get(k) for k in ("date", "description", "amount", "type", "billing_date")})
        else:
            print("   parser do Itaú devolveu vazio (ver o motivo na linha de aviso acima, se houver)")
            print("   linhas com data no começo (descrições mascaradas):")
            for ln in texto.splitlines():
                if re.match(r"^\s*\d{2}/\d{2}\s", ln):
                    print("     ", re.sub(r"\s{2,}", " | ", ln.strip())[:110])
            print("   linhas com 'lançamentos' / 'total':")
            for ln in texto.splitlines():
                if re.search(r"lan[cç]amentos|total d", ln, re.I):
                    print("     ", re.sub(r"\s{2,}", " | ", ln.strip())[:110])
    except ImportError:
        print("\nparser do Itaú: arquivo app/parsers/itau_credito.py não encontrado (pacote 03g não aplicado)")
    r = p._tentar_parser_dedicado(bruto if senha is None else _sem_senha(bruto, senha))
    if not r:
        print("\nparser dedicado: nenhum (cairia no Gemini)")
        return
    print(f"\nparser dedicado: {r.get('bank_detected')} | {len(r['transactions'])} lançamentos")
    for d in r["transactions"]:
        if re.search(r"estorno|anuidade", str(d.get("description")), re.I):
            print("   ", {k: d.get(k) for k in ("date", "description", "amount", "type", "billing_date")})


def _sem_senha(bruto: bytes, senha: str) -> bytes:
    from pypdf import PdfReader, PdfWriter
    rd = PdfReader(io.BytesIO(bruto))
    rd.decrypt(senha)
    w = PdfWriter()
    for pg in rd.pages:
        w.add_page(pg)
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit('uso: python diag_pdf.py "caminho.pdf" [senha]')
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
