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
from importadores.plano_contas import ler_plano_contas
from importadores.empresa_detector import detectar_empresa_pdf

app = FastAPI(title="OrquestraContabil API")

empresas = []
empresa_atual = None

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


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
    palavra: str = ""
    historico: str = ""
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

    negativo = s.startswith("(") and s.endswith(")")

    if "," in s:
        s = s.replace(".", "").replace(",", ".")

    s = re.sub(r"[^0-9\.-]", "", s)

    try:
        n = float(s)
        return -abs(n) if negativo else n
    except Exception:
        return 0.0


def grupo_por_classificacao(classificacao: str, nome: str = ""):
    texto = normalizar(f"{classificacao} {nome}")

    if texto.startswith("1"):
        return "ATIVO"

    if texto.startswith("2"):
        return "PASSIVO"

    if texto.startswith("3") or texto.startswith("4") or texto.startswith("5"):
        return "RESULTADO"

    if "ATIVO" in texto or "BANCO" in texto or "CLIENTE" in texto:
        return "ATIVO"

    if "PASSIVO" in texto or "FORNECEDOR" in texto:
        return "PASSIVO"

    return "RESULTADO"


def natureza_contabil(grupo: str, lado: str):
    grupo = normalizar(grupo)
    lado = normalizar(lado)

    if grupo == "ATIVO":
        return "positivo" if lado == "DEBITO" else "negativo"

    if grupo == "PASSIVO":
        return "negativo" if lado == "DEBITO" else "positivo"

    if grupo == "RESULTADO":
        return "negativo" if lado == "DEBITO" else "positivo"

    return "neutro"


def normalizar_dataframe(df):
    df = df.copy()
    df.columns = [normalizar_coluna(c) for c in df.columns]

    mapa = {}

    for col in df.columns:
        c = col.lower()

        if "data" in c:
            mapa[col] = "data"

        elif any(x in c for x in ["historico", "descricao", "lancamento", "complemento", "cliente", "fornecedor", "razao"]):
            mapa[col] = "historico"

        elif any(x in c for x in ["valor", "vlr", "amount"]):
            mapa[col] = "valor"

        elif any(x in c for x in ["documento", "doc", "numero", "nro"]):
            mapa[col] = "documento"

        elif any(x in c for x in ["classificacao", "classif", "estrutural", "conta_contabil"]):
            mapa[col] = "classificacao"

        elif any(x in c for x in ["descricao_conta", "titulo", "nome", "conta_nome"]):
            mapa[col] = "nome"

        elif any(x in c for x in ["tipo", "analitica", "sintetica", "s_a", "s"]):
            mapa[col] = "tipo"

        elif any(x in c for x in ["codigo", "cod", "reduzido", "conta"]):
            mapa[col] = "codigo"

        elif "grupo" in c:
            mapa[col] = "grupo"

        elif "saldo" in c:
            mapa[col] = "saldo"

        elif "debito" in c:
            mapa[col] = "debito_movimento"

        elif "credito" in c:
            mapa[col] = "credito_movimento"

    df = df.rename(columns=mapa)

    for c in ["codigo", "classificacao", "nome", "tipo", "grupo", "debito", "credito"]:
        if c not in df.columns:
            df[c] = ""

    # Quando Excel vem como: Codigo | S/A | Classificacao | Descricao
    # tenta montar o nome a partir do historico se a coluna nome não existir corretamente.
    if "historico" in df.columns:
        df["nome"] = df["nome"].where(df["nome"].astype(str).str.strip() != "", df["historico"])

    if "valor" in df.columns:
        df["valor"] = df["valor"].apply(numero_br)

    for col in df.columns:
        df[col] = df[col].astype(str).str.strip()

    df["grupo"] = df.apply(lambda r: r["grupo"] or grupo_por_classificacao(r.get("classificacao", ""), r.get("nome", "")), axis=1)
    df["debito"] = df.apply(lambda r: r["debito"] or natureza_contabil(r.get("grupo", ""), "DEBITO"), axis=1)
    df["credito"] = df.apply(lambda r: r["credito"] or natureza_contabil(r.get("grupo", ""), "CREDITO"), axis=1)

    if "tipo" in df.columns:
        def tipo_conta(x):
            x = normalizar(x)
            if x in ["S", "SINTETICA", "SINTETICO"]:
                return "Sintetica"
            if x in ["A", "ANALITICA", "ANALITICO"]:
                return "Analitica"
            return x.title() if x else "Analitica"
        df["tipo"] = df["tipo"].apply(tipo_conta)

    return df


def obter_ou_criar_empresa_auto(dados):
    global empresa_atual

    cnpj = dados.get("cnpj") or ""
    nome = dados.get("nome") or ""

    for emp in empresas:
        if cnpj and emp.get("cnpj") == cnpj:
            emp["codigo"] = dados.get("codigo") or emp.get("codigo")
            emp["periodo"] = dados.get("periodo") or emp.get("periodo")
            if nome:
                emp["nome"] = nome
            empresa_atual = emp
            return emp

    empresa = {
        "id": len(empresas) + 1,
        "codigo": dados.get("codigo"),
        "nome": nome,
        "cnpj": cnpj,
        "periodo": dados.get("periodo"),
        "plano_contas": [],
        "balancetes": [],
        "extratos": [],
        "memoria": [],
        "regras": [],
        "dashboard": {
            "total_contas": 0,
            "total_extratos": 0,
            "total_balancetes": 0,
            "total_conciliacoes": 0
        }
    }

    empresas.append(empresa)
    empresa_atual = empresa
    return empresa


def buscar_conta(contas, termos):
    if isinstance(termos, str):
        termos = [termos]

    for conta in contas:
        nome = normalizar(getattr(conta, "nome", ""))
        classificacao = normalizar(getattr(conta, "classificacao", ""))

        for termo in termos:
            termo_n = normalizar(termo)
            if termo_n and (termo_n in nome or termo_n in classificacao):
                return conta

    return None


def encontrar_melhor_conta(historico, contas):
    melhor = None
    score_melhor = 0
    h = normalizar(historico)

    for conta in contas:
        nome = normalizar(conta.nome)

        if not nome:
            continue

        score = fuzz.partial_ratio(h, nome)

        if score > score_melhor:
            score_melhor = score
            melhor = conta

    return melhor, score_melhor


@app.get("/")
def health():
    return {"status": "online", "sistema": "OrquestraContabil"}


@app.get("/empresas")
async def listar_empresas():
    return empresas


@app.get("/memoria")
async def memoria_empresa():
    global empresa_atual

    if not empresa_atual:
        return []

    return empresa_atual.get("regras", [])


@app.post("/empresa")
async def criar_empresa(dados: dict):
    global empresa_atual

    empresa = {
        "id": len(empresas) + 1,
        "codigo": dados.get("codigo"),
        "nome": dados.get("nome"),
        "cnpj": dados.get("cnpj"),
        "periodo": dados.get("periodo"),
        "plano_contas": [],
        "balancetes": [],
        "extratos": [],
        "memoria": [],
        "regras": [],
        "dashboard": {
            "total_contas": 0,
            "total_extratos": 0,
            "total_balancetes": 0,
            "total_conciliacoes": 0
        }
    }

    empresas.append(empresa)
    empresa_atual = empresa

    return {"sucesso": True, "empresa": empresa}


@app.post("/empresa/selecionar")
async def selecionar_empresa(dados: dict):
    global empresa_atual

    empresa_id = dados.get("id")

    for emp in empresas:
        if emp["id"] == empresa_id:
            empresa_atual = emp
            return {"sucesso": True, "empresa": emp}

    return {"erro": "Empresa não encontrada"}


@app.delete("/empresa/{empresa_id}")
async def apagar_empresa(empresa_id: int):
    global empresa_atual

    for i, emp in enumerate(empresas):
        if emp["id"] == empresa_id:
            apagada = empresas.pop(i)

            if empresa_atual and empresa_atual.get("id") == empresa_id:
                empresa_atual = None

            return {"sucesso": True, "empresa_apagada": apagada}

    return {"erro": "Empresa não encontrada"}


@app.post("/upload-plano")
async def upload_plano(arquivo: UploadFile = File(...)):
    global empresa_atual

    if not empresa_atual:
        return {"erro": "Nenhuma empresa selecionada"}

    sufixo = arquivo.filename.lower().split(".")[-1]

    with tempfile.NamedTemporaryFile(delete=False, suffix=f".{sufixo}") as temp:
        conteudo = await arquivo.read()
        temp.write(conteudo)
        caminho = temp.name

    try:
        if sufixo in ["xlsx", "xls"]:
            df = pd.read_excel(caminho)

        elif sufixo in ["csv", "txt"]:
            try:
                df = ler_plano_contas(caminho)
            except Exception:
                try:
                    df = pd.read_csv(caminho, sep=None, engine="python", encoding="utf-8")
                except UnicodeDecodeError:
                    df = pd.read_csv(caminho, sep=None, engine="python", encoding="latin1")

        elif sufixo == "pdf":
            df = ler_pdf_extrato(caminho)

        else:
            return {"erro": "Formato de plano não suportado"}

        df = normalizar_dataframe(df)
        registros = df.fillna("").to_dict(orient="records")

        empresa_atual["plano_contas"] = registros
        empresa_atual["dashboard"]["total_contas"] = len(registros)

        return {
            "sucesso": True,
            "empresa": empresa_atual["nome"],
            "empresa_atual": empresa_atual,
            "contas": registros
        }

    except Exception as e:
        return {"erro": str(e)}


@app.post("/upload-balancete")
async def upload_balancete(arquivo: UploadFile = File(...)):
    global empresa_atual

    sufixo = arquivo.filename.lower().split(".")[-1]

    with tempfile.NamedTemporaryFile(delete=False, suffix=f".{sufixo}") as temp:
        conteudo = await arquivo.read()
        temp.write(conteudo)
        caminho = temp.name

    try:
        if sufixo == "pdf":
            dados_empresa = detectar_empresa_pdf(caminho)

            if dados_empresa.get("cnpj") or dados_empresa.get("nome"):
                empresa_atual = obter_ou_criar_empresa_auto(dados_empresa)

            df = ler_pdf_extrato(caminho)

        elif sufixo in ["xlsx", "xls"]:
            if not empresa_atual:
                return {"erro": "Nenhuma empresa selecionada"}

            df = pd.read_excel(caminho)

        else:
            if not empresa_atual:
                return {"erro": "Nenhuma empresa selecionada"}

            df = pd.read_csv(caminho, sep=None, engine="python", encoding="latin1")

        df = normalizar_dataframe(df)
        registros = df.fillna("").to_dict(orient="records")

        if empresa_atual:
            empresa_atual["balancetes"] = registros
            empresa_atual["dashboard"]["total_balancetes"] = len(registros)

        return {
            "sucesso": True,
            "empresa": empresa_atual["nome"] if empresa_atual else "",
            "empresa_atual": empresa_atual,
            "balancete": registros
        }

    except Exception as e:
        return {"erro": str(e)}


@app.post("/upload-extrato")
async def upload_extrato(arquivo: UploadFile = File(...)):
    global empresa_atual

    if not empresa_atual:
        return {"erro": "Nenhuma empresa selecionada"}

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

        elif sufixo in ["xlsx", "xls"]:
            df = pd.read_excel(caminho)

        else:
            try:
                df = pd.read_csv(caminho, sep=None, engine="python", encoding="utf-8")
            except UnicodeDecodeError:
                df = pd.read_csv(caminho, sep=None, engine="python", encoding="latin1")

        df = normalizar_dataframe(df)
        registros = df.fillna("").to_dict(orient="records")

        empresa_atual["extratos"] = registros
        empresa_atual["dashboard"]["total_extratos"] = len(registros)

        return {
            "sucesso": True,
            "empresa": empresa_atual["nome"],
            "empresa_atual": empresa_atual,
            "extrato": registros
        }

    except Exception as e:
        return {"erro": str(e)}


@app.post("/conciliar")
def conciliar(payload: ConciliacaoRequest):
    global empresa_atual

    contas = payload.contas
    regras = list(payload.regras)

    if empresa_atual:
        regras += empresa_atual.get("regras", [])

    resultado = []

    for mov in payload.movimentos:
        h = normalizar(mov.historico)

        debito = "Desconhecido"
        credito = "Desconhecido"
        confianca = 40
        observacao = "Sem classificação"

        for regra in regras:
            chave = getattr(regra, "palavra", "") or getattr(regra, "historico", "")

            if chave and normalizar(chave) in h:
                debito = regra.debito
                credito = regra.credito
                confianca = 99
                observacao = "Memória IA"

        conta_similar, score = encontrar_melhor_conta(mov.historico, contas)

        if conta_similar and score >= 80:
            if mov.valor < 0:
                debito = conta_similar.nome
                credito = "Banco"
            else:
                debito = "Banco"
                credito = conta_similar.nome

            confianca = int(score)
            observacao = f"Similaridade {int(score)}%"

        if "COPEL" in h:
            debito = "Energia Elétrica"
            credito = "Banco"
            confianca = 99
            observacao = "Fornecedor reconhecido"

        elif "SANEPAR" in h:
            debito = "Água e Esgoto"
            credito = "Banco"
            confianca = 99
            observacao = "Fornecedor reconhecido"

        elif "TARIFA" in h or "BANCARIA" in h:
            debito = "Despesas Bancárias"
            credito = "Banco"
            confianca = 99
            observacao = "Tarifa bancária reconhecida"

        status = "Conciliado" if confianca >= 90 else "Revisar"

        item = {
            **mov.model_dump(),
            "debito": debito,
            "credito": credito,
            "status": status,
            "confianca": confianca,
            "observacao": observacao
        }

        resultado.append(item)

    if empresa_atual:
        for item in resultado:
            if item["status"] == "Conciliado":
                registro_memoria = {
                    "historico": item["historico"],
                    "palavra": item["historico"],
                    "debito": item["debito"],
                    "credito": item["credito"],
                    "confianca": item["confianca"],
                    "observacao": item["observacao"]
                }

                ja_existe = False

                for r in empresa_atual["regras"]:
                    if normalizar(r.get("historico", "")) == normalizar(item["historico"]):
                        ja_existe = True

                if not ja_existe:
                    empresa_atual["regras"].append(registro_memoria)

        empresa_atual["dashboard"]["total_conciliacoes"] += len(resultado)

    return {"lancamentos": resultado}