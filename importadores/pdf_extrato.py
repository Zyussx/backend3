import pdfplumber
import pandas as pd

def ler_pdf_extrato(caminho):
    linhas = []

    with pdfplumber.open(caminho) as pdf:
        for pagina in pdf.pages:
            texto = pagina.extract_text()

            if not texto:
                continue

            for linha in texto.split("\n"):
                linhas.append(linha)

    return pd.DataFrame({"linha": linhas})
