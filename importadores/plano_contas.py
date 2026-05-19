import re
import pandas as pd
import unicodedata


def normalizar(txt):
    txt = unicodedata.normalize("NFD", str(txt or ""))
    return "".join(ch for ch in txt if unicodedata.category(ch) != "Mn").upper().strip()


def identificar_grupo(classificacao, descricao):
    texto = normalizar(f"{classificacao} {descricao}")

    if texto.startswith("1"):
        return "ATIVO"

    if texto.startswith("2"):
        return "PASSIVO"

    if texto.startswith("3") or texto.startswith("4") or texto.startswith("5"):
        return "RESULTADO"

    return "RESULTADO"


def natureza_contabil(grupo, lado):
    grupo = normalizar(grupo)
    lado = normalizar(lado)

    if grupo == "ATIVO":
        return "positivo" if lado == "DEBITO" else "negativo"

    if grupo == "PASSIVO":
        return "negativo" if lado == "DEBITO" else "positivo"

    if grupo == "RESULTADO":
        return "negativo" if lado == "DEBITO" else "positivo"

    return "neutro"


def separar_classificacao_descricao(texto):
    texto = str(texto or "").strip()
    match = re.match(r"^([\d\.]+)\s+(.*)$", texto)

    if match:
        return match.group(1).strip(), match.group(2).strip()

    return "", texto


def ler_plano_contas(caminho):
    df = pd.read_csv(
        caminho,
        sep=";",
        encoding="latin1",
        engine="python",
        skip_blank_lines=True
    )

    df.columns = [normalizar(c) for c in df.columns]

    contas = []

    for _, row in df.iterrows():
        codigo = str(row.get("CONTA", "")).strip()
        sintetica = str(row.get("S", "")).strip()
        classificacao_raw = str(row.get("CLASSIFICACAO", "")).strip()
        apelido = str(row.get("APELIDO CONTA", "")).strip()

        if not codigo or "____" in classificacao_raw or "PLANO" in normalizar(classificacao_raw):
            continue

        classificacao, descricao = separar_classificacao_descricao(classificacao_raw)

        if not descricao:
            continue

        grupo = identificar_grupo(classificacao, descricao)

        contas.append({
            "codigo": codigo,
            "classificacao": classificacao,
            "nome": descricao,
            "apelido": apelido,
            "tipo": "Sintetica" if normalizar(sintetica) == "S" else "Analitica",
            "grupo": grupo,
            "debito": natureza_contabil(grupo, "DEBITO"),
            "credito": natureza_contabil(grupo, "CREDITO")
        })

    return pd.DataFrame(contas)