#!/usr/bin/env python3
"""Responde mensagens do Telegram com busca de voos. Ex.: 'GRU 10/12 a 20/12' ou 'BSB para LIS em janeiro'.
Roda a cada poucos minutos pelo GitHub Actions (chat.yml)."""
import json, re, unicodedata, urllib.error, urllib.parse
from datetime import date, timedelta
import voos_bot as vb

TOKEN = vb.CFG.get("telegram_token")
CHAT = str(vb.CFG.get("telegram_chat_id", ""))
API = f"https://api.telegram.org/bot{TOKEN}/"

MESES = {"janeiro": 1, "jan": 1, "fevereiro": 2, "fev": 2, "marco": 3, "mar": 3, "abril": 4, "abr": 4,
         "maio": 5, "mai": 5, "junho": 6, "jun": 6, "julho": 7, "jul": 7, "agosto": 8, "ago": 8,
         "setembro": 9, "set": 9, "outubro": 10, "out": 10, "novembro": 11, "nov": 11, "dezembro": 12, "dez": 12}
PARADAS = {"voo", "por", "uma", "com", "que", "dos", "das", "ida", "fim", "dia", "mes", "pra", "vou", "ver",
           "tem", "max", "ate", "gol", "ajuda", "start", "help", "dias", "pro", "nao", "sim", "boa", "oi", "ola"}

HELP = ("Me diga de onde você quer sair e quando. Exemplos:\n"
        "• GRU 10/12 a 20/12\n• BSB em janeiro\n• GIG para LIS 5 a 15 de dezembro\n"
        "• CGB 10/12 a 20/12 máximo 1500\n• GRU em janeiro latam (ou gol, azul)\n\n"
        "Sem datas, uso a janela do config. Sem aeroporto, uso o primeiro da sua lista.")

def norm(s):
    s = unicodedata.normalize("NFD", s.lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")

def resolve(d, m, y=None):
    hoje = date.today()
    try:
        if y is not None:
            return date(y + 2000 if y < 100 else y, m, d)
        dt = date(hoje.year, m, d)
        return dt if dt >= hoje else date(hoje.year + 1, m, d)
    except ValueError:
        return None

def fim_mes(ano, mes):
    return date(ano + (mes == 12), mes % 12 + 1, 1) - timedelta(days=1)

def parse(texto):
    t = norm(texto)
    r = {"orig": None, "dest": None, "ini": None, "fim": None, "teto": None, "cias": set()}
    m = re.search(r"(?:maximo|max|abaixo de|menos de|teto|ate r\$|r\$)\s*(\d[\d\.]*)", t)
    if m:
        r["teto"] = float(m.group(1).replace(".", ""))
        t = t.replace(m.group(0), " ")
    nums = re.findall(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", t)
    mes_re = "|".join(sorted(MESES, key=len, reverse=True))
    if len(nums) >= 2:
        a = resolve(int(nums[0][0]), int(nums[0][1]), int(nums[0][2]) if nums[0][2] else None)
        b = resolve(int(nums[1][0]), int(nums[1][1]), int(nums[1][2]) if nums[1][2] else None)
        if a and b:
            if b < a:
                b = resolve(b.day, b.month, b.year + 1)
            r["ini"], r["fim"] = a, b
        t = re.sub(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b", " ", t)
    else:
        m = re.search(rf"\b(\d{{1,2}})\s*de\s*({mes_re})\s*(?:a|ate|ao)\s*(\d{{1,2}})\s*de\s*({mes_re})\b", t)
        m2 = re.search(rf"\b(\d{{1,2}})\s*(?:a|ate|ao|-)\s*(\d{{1,2}})\s*de\s*({mes_re})\b", t)
        m3 = re.search(rf"\b({mes_re})\b", t)
        if m:
            r["ini"] = resolve(int(m.group(1)), MESES[m.group(2)])
            r["fim"] = resolve(int(m.group(3)), MESES[m.group(4)])
            if r["ini"] and r["fim"] and r["fim"] < r["ini"]:
                r["fim"] = resolve(r["fim"].day, r["fim"].month, r["fim"].year + 1)
            t = t.replace(m.group(0), " ")
        elif m2:
            r["ini"] = resolve(int(m2.group(1)), MESES[m2.group(3)])
            r["fim"] = resolve(int(m2.group(2)), MESES[m2.group(3)])
            t = t.replace(m2.group(0), " ")
        elif m3:
            mes = MESES[m3.group(1)]
            ini = resolve(1, mes)
            if ini:
                if ini < date.today():
                    ini = date.today()
                r["ini"], r["fim"] = ini, fim_mes(resolve(1, mes).year, mes)
            t = t.replace(m3.group(0), " ")
    for palavra in ("latam", "gol", "azul"):
        if re.search(rf"\b{palavra}\b", t):
            r["cias"].add(palavra)
            t = re.sub(rf"\b{palavra}\b", " ", t)
    codigos = [c.upper() for c in re.findall(r"\b[a-z]{3}\b", t) if c not in PARADAS and c not in MESES]
    if codigos:
        r["orig"] = codigos[0]
    if len(codigos) > 1:
        r["dest"] = codigos[1]
    return r

def meses_de(ini, fim):
    y, m = ini.year, ini.month
    for _ in range(12):
        if (y, m) > (fim.year, fim.month):
            break
        yield f"{y}-{m:02d}"
        m += 1
        if m > 12:
            y, m = y + 1, 1

def buscar(orig, dest, ini, fim, teto, cias=None):
    achados = []
    for mes in meses_de(ini, fim):
        p = {"origin": orig, "departure_at": mes, "one_way": "false", "sorting": "price", "currency": "brl"}
        if dest:
            p["destination"] = dest
        else:
            p["unique"] = "false"
        dados = vb.consulta(p)
        for v in dados.get("data", []):
            if not v.get("return_at") or not vb.cia_ok(v.get("airline", ""), cias):
                continue
            ida, volta = date.fromisoformat(v["departure_at"][:10]), date.fromisoformat(v["return_at"][:10])
            if ida < ini or volta > fim or (teto and v["price"] > teto):
                continue
            lk = v.get("link", "")
            achados.append({"dest": v["destination"], "preco": v["price"], "ida": ida, "volta": volta,
                            "cia": vb.cia_nome(v.get("airline", "")),
                            "link": "https://www.aviasales.com" + lk if lk.startswith("/") else ""})
    if dest:
        vistos, lista = set(), []
        for v in sorted(achados, key=lambda x: x["preco"]):
            if (v["ida"], v["volta"]) not in vistos:
                vistos.add((v["ida"], v["volta"]))
                lista.append(v)
        return lista[:6]
    melhor = {}
    for v in achados:
        if v["dest"] not in melhor or v["preco"] < melhor[v["dest"]]["preco"]:
            melhor[v["dest"]] = v
    return sorted(melhor.values(), key=lambda x: x["preco"])[:10]

def responder(texto):
    t = norm(texto).strip()
    if t in ("/start", "start", "ajuda", "/ajuda", "help", "/help", "oi", "ola"):
        return HELP
    q = parse(texto)
    orig = q["orig"] or vb.AEROPORTOS[0]
    hoje = date.today()
    ini = max(q["ini"] or date.fromisoformat(vb.INICIO), hoje)
    fim = q["fim"] or date.fromisoformat(vb.FIM)
    if fim < ini:
        return "Não entendi as datas. Exemplo: GRU 10/12 a 20/12"
    voos = buscar(orig, q["dest"], ini, fim, q["teto"], q["cias"] or None)
    cab = f"✈️ {orig} → {q['dest'] or 'qualquer destino'} · {ini:%d/%m} a {fim:%d/%m}"
    if q["cias"]:
        cab += " · " + "/".join(sorted(c.upper() if c == "gol" else c.capitalize() for c in q["cias"]))
    if q["teto"]:
        cab += f" · até R$ {vb.brl(q['teto'])}"
    if not voos:
        return cab + "\n\nNão achei voos nesse período. Tente ampliar as datas ou o valor máximo."
    linhas = [cab, ""]
    for i, v in enumerate(voos, 1):
        linhas.append(f"{i}. {v['dest']} · R$ {vb.brl(v['preco'])} · {v['ida']:%d/%m} → {v['volta']:%d/%m} · {v['cia']}")
        if v["link"]:
            linhas.append(v["link"])
    return "\n".join(linhas)

def enviar(texto):
    body = urllib.parse.urlencode({"chat_id": CHAT, "text": texto, "disable_web_page_preview": "true"}).encode()
    vb.http(API + "sendMessage", body)

def main():
    if not TOKEN or not CHAT:
        print("Sem token/chat_id do Telegram.")
        return
    ups = json.loads(vb.http(API + "getUpdates?timeout=0")).get("result", [])
    for u in ups:
        msg = u.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != CHAT or not msg.get("text"):
            continue
        try:
            enviar(responder(msg["text"]))
        except Exception as e:
            print("Erro:", e)
            enviar("Deu erro na busca. Tente de novo em alguns minutos.")
    if ups:
        vb.http(API + f"getUpdates?offset={ups[-1]['update_id'] + 1}&timeout=0")
    print("ok", len(ups), "mensagens")

if __name__ == "__main__":
    main()
