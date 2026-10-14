#!/usr/bin/env python3
"""Rastreador de voos baratos (dinheiro) + promoções de transferência de pontos.
Roda uma vez por execução; agende no cron / GitHub Actions / PythonAnywhere."""
import html, json, os, urllib.error, sqlite3, statistics, sys, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime
from pathlib import Path

BASE = Path(__file__).parent
CFG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
# Segredos podem vir de variáveis de ambiente (GitHub Actions)
for chave, env in [("travelpayouts_token", "TRAVELPAYOUTS_TOKEN"), ("telegram_token", "TELEGRAM_TOKEN"),
                   ("telegram_chat_id", "TELEGRAM_CHAT_ID"), ("aeroporto_saida", "AEROPORTO_SAIDA")]:
    if os.environ.get(env):
        CFG[chave] = os.environ[env]
DRY = "--dry" in sys.argv or not CFG.get("telegram_token")
db = sqlite3.connect(BASE / "voos.db")
db.executescript("""
CREATE TABLE IF NOT EXISTS historico(destino TEXT, dia TEXT, preco REAL);
CREATE TABLE IF NOT EXISTS avisados(chave TEXT PRIMARY KEY);
""")

OFERTAS, PROMOS = [], []

def http(url, data=None):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "Mozilla/5.0 voos-bot"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()

def avisar(texto):
    if DRY:
        print("\n[AVISO]", texto)
        return
    url = f"https://api.telegram.org/bot{CFG['telegram_token']}/sendMessage"
    body = urllib.parse.urlencode({"chat_id": CFG["telegram_chat_id"], "text": texto,
                                   "disable_web_page_preview": "true"}).encode()
    http(url, body)

def brl(x):
    return f"{x:,.0f}".replace(",", ".")

def novo(chave):
    if db.execute("SELECT 1 FROM avisados WHERE chave=?", (chave,)).fetchone():
        return False
    db.execute("INSERT INTO avisados VALUES (?)", (chave,))
    return True

# ---------- 1) Voos em dinheiro (ida e volta, qualquer destino) ----------
def meses(n):
    h = date.today()
    for i in range(n):
        m = h.month - 1 + i
        yield f"{h.year + m // 12}-{m % 12 + 1:02d}"

def buscar_dinheiro():
    achados = {}
    for mes in meses(CFG.get("meses_a_frente", 6)):
        q = urllib.parse.urlencode({
            "origin": CFG["aeroporto_saida"], "departure_at": mes,
            "one_way": "false", "unique": "true", "sorting": "price", "limit": 50,
            "currency": "brl", "token": CFG["travelpayouts_token"]})
        try:
            dados = json.loads(http("https://api.travelpayouts.com/aviasales/v3/prices_for_dates?" + q))
        except urllib.error.HTTPError as err:
            print("Erro Travelpayouts", mes, err.code, err.read().decode(errors="ignore")[:300])
            continue
        for v in dados.get("data", []):
            if not v.get("return_at"):
                continue
            dias = (datetime.fromisoformat(v["return_at"][:10]) - datetime.fromisoformat(v["departure_at"][:10])).days
            if not CFG["dias_min"] <= dias <= CFG["dias_max"]:
                continue
            d = v["destination"]
            if d not in achados or v["price"] < achados[d]["price"]:
                achados[d] = v
    return achados

def checar_dinheiro():
    hoje = date.today().isoformat()
    for dest, v in buscar_dinheiro().items():
        preco = v["price"]
        hist = [r[0] for r in db.execute("SELECT preco FROM historico WHERE destino=?", (dest,))]
        db.execute("INSERT INTO historico VALUES (?,?,?)", (dest, hoje, preco))
        teto = CFG.get("teto_por_destino", {}).get(dest, CFG.get("teto_geral"))
        barato_hist = len(hist) >= 5 and preco <= statistics.median(hist) * CFG.get("fracao_da_mediana", 0.75)
        barato_teto = teto is not None and preco <= teto
        OFERTAS.append({"dest": dest, "preco": preco, "ida": v["departure_at"][:10], "volta": v["return_at"][:10],
                        "cia": v.get("airline", ""), "link": "https://www.aviasales.com" + v.get("link", ""),
                        "barato": bool(barato_hist or barato_teto)})
        if (barato_hist or barato_teto) and novo(f"{dest}|{v['departure_at'][:10]}|{int(preco)}"):
            motivo = "abaixo do seu teto" if barato_teto else "bem abaixo do preço normal"
            avisar(f"✈️ {CFG['aeroporto_saida']} → {dest} por R$ {brl(preco)} ({motivo})\n"
                   f"Ida {v['departure_at'][:10]} · Volta {v['return_at'][:10]} · {v.get('airline','')}\n"
                   f"https://www.aviasales.com{v.get('link','')}")
    db.commit()

# ---------- 2) Promoções de transferência de pontos ----------
def checar_promos():
    primeira_vez = db.execute("SELECT COUNT(*) FROM avisados WHERE chave LIKE 'promo|%'").fetchone()[0] == 0
    palavras = [p.lower() for p in CFG["palavras_promo"]]
    for feed in CFG["feeds_promo"]:
        try:
            raiz = ET.fromstring(http(feed))
        except Exception as e:
            print("Falha no feed", feed, e)
            continue
        for item in raiz.iter("item"):
            titulo = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            if not any(p in titulo.lower() for p in palavras):
                continue
            if len(PROMOS) < 20 and link not in [x["link"] for x in PROMOS]:
                PROMOS.append({"titulo": titulo, "link": link})
            if novo("promo|" + link) and not primeira_vez:
                avisar(f"💳 Promoção de pontos: {titulo}\n{link}")
    db.commit()


# ---------- 3) App (site instalável no celular) ----------
def gerar_site():
    out = BASE / "docs"
    out.mkdir(exist_ok=True)
    e = html.escape
    ofertas = sorted(OFERTAS, key=lambda o: (not o["barato"], o["preco"]))
    cards = "".join(
        f'<a class="card{" hot" if o["barato"] else ""}" href="{e(o["link"])}">'
        f'<b>{e(CFG["aeroporto_saida"])} → {e(o["dest"])}</b>'
        f'<span class="p">R$ {brl(o["preco"])}</span>'
        f'<small>{o["ida"]} → {o["volta"]} · {e(o["cia"])}{" · 🔥 barato" if o["barato"] else ""}</small></a>'
        for o in ofertas) or "<p>Nenhum voo encontrado ainda.</p>"
    promos = "".join(f'<a class="card" href="{e(p["link"])}"><b>{e(p["titulo"])}</b></a>' for p in PROMOS) \
        or "<p>Nenhuma promoção recente.</p>"
    agora = datetime.now().strftime("%d/%m/%Y %H:%M")
    (out / "index.html").write_text(f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Voos baratos</title><link rel="manifest" href="manifest.json"><link rel="icon" href="icon.svg">
<meta name="theme-color" content="#0b5fff"><meta name="apple-mobile-web-app-capable" content="yes">
<style>:root{{--bg:#f6f7f9;--fg:#14171a;--card:#fff;--mut:#667}}
@media(prefers-color-scheme:dark){{:root{{--bg:#101215;--fg:#eceff3;--card:#1a1d22;--mut:#9aa}}}}
body{{margin:0;padding:env(safe-area-inset-top) 14px 30px;background:var(--bg);color:var(--fg);font:16px system-ui,sans-serif}}
h1{{font-size:22px}}h2{{font-size:17px;margin-top:26px}}.card{{display:block;background:var(--card);color:inherit;
text-decoration:none;border-radius:12px;padding:12px 14px;margin:8px 0;position:relative}}
.hot{{border:2px solid #ff7a1a}}.p{{position:absolute;right:14px;top:12px;font-weight:700}}small{{display:block;color:var(--mut);margin-top:4px}}
.up{{color:var(--mut);font-size:13px}}</style></head><body>
<h1>✈️ Voos saindo de {e(CFG["aeroporto_saida"])}</h1><div class="up">Atualizado em {agora}</div>
<h2>Ida e volta mais baratas</h2>{cards}<h2>💳 Promoções de pontos</h2>{promos}</body></html>""", encoding="utf-8")
    (out / "manifest.json").write_text(json.dumps({"name": "Voos baratos", "short_name": "Voos", "start_url": ".",
        "display": "standalone", "background_color": "#0b5fff", "theme_color": "#0b5fff",
        "icons": [{"src": "icon.svg", "sizes": "any", "type": "image/svg+xml"}]}), encoding="utf-8")
    (out / "icon.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><rect width="100" height="100" fill="#0b5fff"/>'
        '<text x="50" y="68" font-size="56" text-anchor="middle">✈️</text></svg>', encoding="utf-8")

if __name__ == "__main__":
    checar_promos()
    if CFG.get("travelpayouts_token"):
        checar_dinheiro()
    gerar_site()
    print("ok", datetime.now().isoformat(timespec="seconds"))
