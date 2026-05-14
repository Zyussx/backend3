from rapidfuzz import fuzz
from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List
import tempfile
import pandas as pd
import unicodedata
import re

from importadores.ofx_reader import ler_ofx
from importadores.pdf_extrato import ler_pdf_extrato

app = FastAPI(title="OrquestraContabil API")  


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

class Conta(BaseModel):
    codigo: str = ""
    classificacao: str = ""
    nome: str = ""
    tipo: str = "Analitica"
    grupo: str = ""

class Movimento(BaseModel):
    data: str = ""
    historico: str = ""
    valor: float = 0
    documento: str = ""

class Regra(BaseModel):
    palavra: str
    debito: str
    credito: str
    observacao: str = ""

class ConciliacaoRequest(BaseModel):
    contas: List[Conta]
    movimentos: List[Movimento]
    regras: List[Regra] = []

def normalizar(txt: str) -> str:
    txt = unicodedata.normalize("NFD", str(txt or ""))
    return "".join(ch for ch in txt if unicodedata.category(ch) != "Mn").upper().strip()

def identificar_grupo_conta(conta):
    texto = normalizar(
        f"{getattr(conta, 'codigo', '')} {getattr(conta, 'classificacao', '')} {getattr(conta, 'nome', '')} {getattr(conta, 'grupo', '')}"
    )

    if texto.startswith("1") or "ATIVO" in texto or "BANCO" in texto or "CLIENTE" in texto:
        return "ATIVO"

    if texto.startswith("2") or "PASSIVO" in texto or "FORNECEDOR" in texto:
        return "PASSIVO"

    if (
        texto.startswith("3")
        or texto.startswith("4")
        or texto.startswith("5")
        or "RESULTADO" in texto
        or "RECEITA" in texto
        or "DESPESA" in texto
        or "CUSTO" in texto
    ):
        return "RESULTADO"

    return "RESULTADO"


def sinal_contabil(conta, natureza):
    grupo = identificar_grupo_conta(conta)

    if grupo == "ATIVO":
        return "positivo" if natureza == "debito" else "negativo"

    if grupo == "PASSIVO":
        return "negativo" if natureza == "debito" else "positivo"

    if grupo == "RESULTADO":
        return "negativo" if natureza == "debito" else "positivo"

    return "neutro"


def procurar_conta_exata_ou_nome(contas, nome_conta):
    conta = buscar_conta(contas, [nome_conta])
    if conta:
        return conta
    return Conta(nome=nome_conta)


def normalizar_coluna(txt: str) -> str:
    t = normalizar(txt).lower()
    t = t.replace(" ", "_").replace("/", "_").replace("-", "_")
    return re.sub(r"[^a-z0-9_]", "", t)

def numero_br(valor):
    if valor is None:
        return 0.0
    s = str(valor).strip().replace("R$", "").replace(" ", "")
    if not s:
        return 0.0
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    s = re.sub(r"[^0-9\.-]", "", s)
    try:
        return float(s)
    except Exception:
        return 0.0

def buscar_conta(contas, termos):
    if isinstance(termos, str):
        termos = [termos]
    for conta in contas:
        nome = normalizar(conta.nome)
        classificacao = normalizar(conta.classificacao)
        for termo in termos:
            termo_n = normalizar(termo)
            if termo_n and (termo_n in nome or termo_n in classificacao):
                return conta
    return None

def encontrar_melhor_conta(historico, contas):
    melhor_conta = None
    melhor_score = 0
    h = normalizar(historico)
    for conta in contas:
        nome = normalizar(conta.nome)
        if not nome:
            continue
        score = fuzz.partial_ratio(h, nome)
        if score > melhor_score:
            melhor_score = score
            melhor_conta = conta
    return melhor_conta, melhor_score

def conta_banco(contas: List[Conta], movimentos: List[Movimento]):
    historicos = " ".join([normalizar(m.historico) for m in movimentos])
    bancos = {
        "ITAU": ["ITAU", "ITAU UNIBANCO"],
        "SICREDI": ["SICREDI"],
        "BRADESCO": ["BRADESCO"],
        "CAIXA": ["CAIXA"],
        "BANCO DO BRASIL": ["BANCO DO BRASIL", "BB"]
    }
    for banco, palavras in bancos.items():
        for palavra in palavras:
            if normalizar(palavra) in historicos:
                conta = buscar_conta(contas, [banco, palavra])
                if conta:
                    return conta
    return buscar_conta(contas, ["BANCO", "CONTA CORRENTE", "CAIXA E EQUIVALENTES"]) or Conta(nome="Banco", grupo="Ativo")

def aplicar_regra(historico, regras):
    h = normalizar(historico)
    for regra in regras:
        if normalizar(regra.palavra) in h:
            return regra
    return None

def normalizar_dataframe(df: pd.DataFrame):
    df = df.copy()
    df.columns = [normalizar_coluna(c) for c in df.columns]
    mapa = {}
    for col in df.columns:
        c = col.lower()
        if c == "data" or c.startswith("data"):
            mapa[col] = "data"
        elif any(x in c for x in ["historico", "hist", "lancamento", "descricao", "cliente_ou_fornecedor", "cliente_fornecedor", "fornecedor", "cliente", "razao_social"]):
            mapa[col] = "historico"
        elif any(x in c for x in ["valor", "amount", "vlr"]):
            mapa[col] = "valor"
        elif any(x in c for x in ["documento", "doc", "numero"]):
            mapa[col] = "documento"
        elif "saldo" in c:
            mapa[col] = "saldo"
        elif any(x in c for x in ["codigo", "cod"]):
            mapa[col] = "codigo"
        elif any(x in c for x in ["classificacao", "classif", "conta_contabil"]):
            mapa[col] = "classificacao"
        elif any(x in c for x in ["nome", "conta", "descricao_da_conta"]):
            mapa[col] = "nome"
        elif any(x in c for x in ["grupo", "tipo"]):
            mapa[col] = "grupo"
    df = df.rename(columns=mapa)
    if "linha" in df.columns and len(df.columns) == 1:
        linhas = []
        for raw in df["linha"].astype(str).tolist():
            linha = raw.strip()
            m_data = re.search(r"(\d{2}/\d{2}(?:/\d{4})?)", linha)
            valores = re.findall(r"[-]?\d{1,3}(?:\.\d{3})*,\d{2}", linha)
            if m_data and valores:
                valor = numero_br(valores[-2] if len(valores) >= 2 else valores[-1])
                saldo = valores[-1] if len(valores) >= 2 else ""
                historico = linha.replace(m_data.group(1), "").strip()
                for v in valores:
                    historico = historico.replace(v, "").strip()
                linhas.append({"data": m_data.group(1), "historico": historico, "valor": valor, "documento": "", "saldo": saldo})
        if linhas:
            return pd.DataFrame(linhas)
    if "valor" in df.columns:
        df["valor"] = df["valor"].apply(numero_br)
    return df

@app.get("/")
def health():
    return {"status": "online", "sistema": "OrquestraContabil"}

@app.post("/conciliar")
def conciliar(payload: ConciliacaoRequest):
    contas = payload.contas
    regras = payload.regras
    banco = conta_banco(contas, payload.movimentos)
    resultado = []
    for mov in payload.movimentos:
        h = normalizar(mov.historico)
        regra = aplicar_regra(mov.historico, regras)
        if regra:
            resultado.append({**mov.model_dump(), "debito": regra.debito, "credito": regra.credito, "status": "Conciliado", "confianca": 99, "observacao": "Classificacao reaproveitada pela memoria da empresa."})
            continue
        conta_similar, score_similar = encontrar_melhor_conta(mov.historico, contas)
        if mov.valor >= 0:
            debito = banco.nome
            conta_credito = buscar_conta(contas, ["CLIENTE", "RECEITA", "VENDAS", "SERVICO"])
            credito = conta_credito.nome if conta_credito else "Clientes/Receita"
            confianca = 82
            observacao = "Entrada bancaria identificada."
        else:
            credito = banco.nome
            conta_debito = buscar_conta(contas, ["FORNECEDOR", "DESPESA", "CUSTO"])
            debito = conta_debito.nome if conta_debito else "Fornecedores/Despesa"
            confianca = 70
            observacao = "Saida bancaria identificada."
        if "COPEL" in h:
            conta = buscar_conta(contas, ["COPEL", "ENERGIA"])
            debito = conta.nome if conta else "Energia Eletrica"
            confianca = 98
            observacao = "Fornecedor COPEL reconhecido."
        elif "SANEPAR" in h:
            conta = buscar_conta(contas, ["SANEPAR", "AGUA"])
            debito = conta.nome if conta else "SANEPAR"
            confianca = 96
            observacao = "Fornecedor SANEPAR reconhecido."
        elif "TARIFA" in h or "BANCARIA" in h:
            conta = buscar_conta(contas, ["DESPESAS BANCARIAS", "TARIFAS", "BANCARIAS"])
            debito = conta.nome if conta else "Despesas Bancarias"
            confianca = 99
            observacao = "Tarifa bancaria reconhecida."
        elif "ALUGUEL" in h:
            conta = buscar_conta(contas, ["ALUGUEL"])
            debito = conta.nome if conta else "Aluguel"
            confianca = 95
            observacao = "Despesa de aluguel reconhecida."
        elif "FACEBOOK" in h or "META" in h:
            conta = buscar_conta(contas, ["PUBLICIDADE", "PROPAGANDA", "MARKETING"])
            debito = conta.nome if conta else "Publicidade e Propaganda"
            confianca = 95
            observacao = "Publicidade reconhecida pelo historico."
        elif "SERVICO" in h or "RECEBIMENTO" in h or "NF" in h:
            conta = buscar_conta(contas, ["RECEITA", "SERVICO"])
            credito = conta.nome if conta else "Receita de Servicos"
            confianca = 92
            observacao = "Receita de servico reconhecida."
        if conta_similar and score_similar >= 75:
            if mov.valor >= 0:
                credito = conta_similar.nome
            else:
                debito = conta_similar.nome
            confianca = max(confianca, int(score_similar))
            observacao = f"Conta encontrada por similaridade ({int(score_similar)}%)."
        status = "Conciliado" if confianca >= 90 else ("Revisar" if confianca >= 75 else "Pendente")

        conta_debito_obj = procurar_conta_exata_ou_nome(contas, debito)
        conta_credito_obj = procurar_conta_exata_ou_nome(contas, credito)

        resultado.append({
            **mov.model_dump(),
            "debito": debito,
            "credito": credito,
            "status": status,
            "confianca": int(confianca),
            "observacao": observacao,
            "grupo_debito": identificar_grupo_conta(conta_debito_obj),
            "grupo_credito": identificar_grupo_conta(conta_credito_obj),
            "sinal_debito": sinal_contabil(conta_debito_obj, "debito"),
            "sinal_credito": sinal_contabil(conta_credito_obj, "credito")
        })
    return {"lancamentos": resultado}

@app.post("/upload-extrato")
async def upload_extrato(arquivo: UploadFile = File(...)):
    sufixo = arquivo.filename.lower().split(".")[-1]
    with tempfile.NamedTemporaryFile(delete=False, suffix=f".{sufixo}") as temp:
        conteudo = await arquivo.read()
        temp.write(conteudo)
        caminho = temp.name
    try:
        if sufixo == "ofx":
            df = ler_ofx(caminho)
        elif sufixo == "pdf":
            df = ler_pdf_extrato(caminho)
        elif sufixo in ["csv", "txt"]:
            try:
                df = pd.read_csv(caminho, sep=None, engine="python", encoding="utf-8")
            except UnicodeDecodeError:
                df = pd.read_csv(caminho, sep=None, engine="python", encoding="latin1")
        else:
            return {"erro": "Formato não suportado"}
        df = normalizar_dataframe(df)
        return {"sucesso": True, "linhas": df.fillna("").to_dict(orient="records")}
    except Exception as e:
        return {"erro": str(e)}
