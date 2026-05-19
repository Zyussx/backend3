import re
import pdfplumber


def detectar_empresa_pdf(caminho):
    texto = ""

    with pdfplumber.open(caminho) as pdf:
        for page in pdf.pages[:2]:
            texto += "\n" + (page.extract_text() or "")

    empresa_codigo = ""
    empresa_nome = ""
    cnpj = ""
    periodo = ""

    m_empresa = re.search(r"Empresa\s+(\d+)\s+-\s+(.+)", texto)
    if m_empresa:
        empresa_codigo = m_empresa.group(1).strip()
        empresa_nome = m_empresa.group(2).strip()

    m_cnpj = re.search(r"CNPJ:\s*([\d\.\-/]+)", texto)
    if m_cnpj:
        cnpj = m_cnpj.group(1).strip()

    m_periodo = re.search(r"Período:\s*([0-9/]+)\s*a\s*([0-9/]+)", texto)
    if m_periodo:
        periodo = f"{m_periodo.group(1)} a {m_periodo.group(2)}"

    return {
        "codigo": empresa_codigo,
        "nome": empresa_nome,
        "cnpj": cnpj,
        "periodo": periodo,
        "texto_detectado": texto[:1000]
    }