from ofxparse import OfxParser
import pandas as pd

def ler_ofx(caminho):
    with open(caminho, 'rb') as arquivo:
        ofx = OfxParser.parse(arquivo)

    transacoes = []

    for transacao in ofx.account.statement.transactions:
        transacoes.append({
            "data": transacao.date.strftime("%d/%m/%Y"),
            "historico": transacao.memo,
            "valor": float(transacao.amount),
            "documento": str(transacao.id)
        })

    return pd.DataFrame(transacoes)