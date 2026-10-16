#!/usr/bin/env python3
"""Rastreador de voos baratos (dinheiro) + promoções de transferência de pontos.
Gera o app (docs/index.html) e avisa pelo Telegram. Rode via GitHub Actions."""
import html, json, os, sqlite3, statistics, sys, urllib.error, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path

BASE = Path(__file__).parent
CFG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
for chave, env in [("travelpayouts_token", "TRAVELPAYOUTS_TOKEN"), ("telegram_token", "TELEGRAM_TOKEN"),
                   ("telegram_chat_id", "TELEGRAM_CHAT_ID")]:
    if os.environ.get(env):
        CFG[chave] = os.environ[env]
if os.environ.get("AEROPORTOS_SAIDA"):
    CFG["aeroportos_saida"] = os.environ["AEROPORTOS_SAIDA"].split(",")

AEROPORTOS = [a.strip().upper() for a in (CFG.get("aeroportos_saida") or [CFG.get("aeroporto_saida", "GRU")]) if a.strip()]
HOJE = date.today().isoformat()
INICIO = max(CFG.get("data_inicio") or HOJE, HOJE)
FIM = CFG.get("data_fim") or (date.today() + timedelta(days=183)).isoformat()

DRY = "--dry" in sys.argv or not CFG.get("telegram_token")
db = sqlite3.connect(BASE / "voos.db")
db.executescript("""
CREATE TABLE IF NOT EXISTS historico(destino TEXT, dia TEXT, preco REAL);
CREATE TABLE IF NOT EXISTS avisados(chave TEXT PRIMARY KEY);
""")

OFERTAS, PROMOS = [], []

COMPANHIAS = {"LA": "LATAM", "JJ": "LATAM", "G3": "GOL", "AD": "Azul"}
PERMITIDAS = {n.lower() for n in CFG.get("somente_companhias", ["LATAM", "GOL", "Azul"])}
API_PRECOS = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates?"

def cia_nome(cod):
    return COMPANHIAS.get(cod, cod)

def cia_ok(cod, permitidas=None):
    p = PERMITIDAS if permitidas is None else permitidas
    return not p or cia_nome(cod).lower() in p

def consulta(params):
    params = dict(params, token=CFG["travelpayouts_token"])
    for limite in (1000, 100):
        params["limit"] = limite
        try:
            return json.loads(http(API_PRECOS + urllib.parse.urlencode(params)))
        except urllib.error.HTTPError as err:
            print("Erro Travelpayouts", err.code, err.read().decode(errors="ignore")[:300])
            if err.code != 400:
                break
    return {}

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
def meses():
    a, b = date.fromisoformat(INICIO), date.fromisoformat(FIM)
    y, m = a.year, a.month
    for _ in range(12):
        if (y, m) > (b.year, b.month):
            break
        yield f"{y}-{m:02d}"
        m += 1
        if m > 12:
            y, m = y + 1, 1

def buscar_dinheiro():
    melhores = {}
    for orig in AEROPORTOS:
        for mes in meses():
            dados = consulta({"origin": orig, "departure_at": mes, "one_way": "false", "unique": "false",
                              "sorting": "price", "currency": "brl"})
            for v in dados.get("data", []):
                if not v.get("return_at") or not cia_ok(v.get("airline", "")):
                    continue
                ida, volta = v["departure_at"][:10], v["return_at"][:10]
                if ida < INICIO or volta > FIM:
                    continue
                dias = (date.fromisoformat(volta) - date.fromisoformat(ida)).days
                if not CFG["dias_min"] <= dias <= CFG["dias_max"]:
                    continue
                lk = v.get("link", "")
                voo = {"orig": orig, "dest": v["destination"], "preco": v["price"], "ida": ida, "volta": volta,
                       "cia": cia_nome(v.get("airline", "")),
                       "link": "https://www.aviasales.com" + lk if lk.startswith("/") else ""}
                chave = (orig, voo["dest"], voo["cia"], ida[:7])
                if chave not in melhores or voo["preco"] < melhores[chave]["preco"]:
                    melhores[chave] = voo
    return list(melhores.values())

def checar_dinheiro():
    rotas = {}
    for v in buscar_dinheiro():
        rotas.setdefault(f"{v['orig']}-{v['dest']}", []).append(v)
    for rota, lista in rotas.items():
        melhor = min(lista, key=lambda v: v["preco"])
        hist = [r[0] for r in db.execute("SELECT preco FROM historico WHERE destino=?", (rota,))]
        db.execute("INSERT INTO historico VALUES (?,?,?)", (rota, HOJE, melhor["preco"]))
        teto = CFG.get("teto_por_destino", {}).get(melhor["dest"], CFG.get("teto_geral"))
        mediana = statistics.median(hist) if len(hist) >= 5 else None
        for v in lista:
            v["barato"] = bool((mediana is not None and v["preco"] <= mediana * CFG.get("fracao_da_mediana", 0.75))
                               or (teto is not None and v["preco"] <= teto))
        OFERTAS.extend(lista)
        if melhor["barato"] and novo(f"{rota}|{melhor['ida']}|{int(melhor['preco'])}"):
            motivo = "abaixo do seu teto" if teto is not None and melhor["preco"] <= teto else "bem abaixo do preço normal"
            avisar(f"✈️ {melhor['orig']} → {melhor['dest']} por R$ {brl(melhor['preco'])} ({motivo})\n"
                   f"Ida {melhor['ida']} · Volta {melhor['volta']} · {melhor['cia']}\n{melhor['link']}")
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
PAGINA = """<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Voos baratos</title><link rel="manifest" href="manifest.json"><link rel="icon" href="icon.svg">
<meta name="theme-color" content="#0b5fff"><meta name="apple-mobile-web-app-capable" content="yes">
<style>:root{--bg:#f6f7f9;--fg:#14171a;--card:#fff;--mut:#667;--bd:#d5d9df}
@media(prefers-color-scheme:dark){:root{--bg:#101215;--fg:#eceff3;--card:#1a1d22;--mut:#9aa;--bd:#333943}}
body{margin:0;padding:env(safe-area-inset-top) 14px 30px;background:var(--bg);color:var(--fg);font:16px system-ui,sans-serif}
h1{font-size:22px}h2{font-size:17px;margin-top:26px}
.card{display:block;background:var(--card);color:inherit;text-decoration:none;border-radius:12px;padding:12px 14px;margin:8px 0;position:relative}
.hot{border:2px solid #ff7a1a}.p{position:absolute;right:14px;top:12px;font-weight:700}
small{display:block;color:var(--mut);margin-top:4px}.up{color:var(--mut);font-size:13px}
.f{display:grid;grid-template-columns:1fr 1fr;gap:10px;background:var(--card);padding:12px;border-radius:12px}
.f label{font-size:13px;color:var(--mut);display:block}
.f select,.f input[type=date]{width:100%;box-sizing:border-box;margin-top:3px;padding:8px;font-size:16px;color:var(--fg);background:var(--bg);border:1px solid var(--bd);border-radius:8px}
.f .w{grid-column:1/3}</style></head><body>
<h1>✈️ Voos baratos</h1><div class="up" id="up"></div>
<div class="f">
<div class="w"><label>Saindo de</label><select id="orig"></select></div>
<div class="w"><label>Companhia</label><select id="cia"></select></div>
<div><label>Ida a partir de</label><input type="date" id="de"></div>
<div><label>Volta até</label><input type="date" id="ate"></div>
<div class="w"><label><input type="checkbox" id="hot"> Mostrar só os 🔥 baratos</label></div>
</div>
<h2>Ida e volta mais baratas</h2><div id="lista"></div>
<h2>💳 Promoções de pontos</h2>__PROMOS__
<h2>Sites das companhias</h2>
<a class="card" href="https://www.latamairlines.com/br/pt"><b>LATAM</b></a>
<a class="card" href="https://www.voegol.com.br"><b>GOL</b></a>
<a class="card" href="https://www.voeazul.com.br"><b>Azul</b></a>
<script>
const D=__DATA__;
const $=id=>document.getElementById(id);
const fmt=n=>'R$ '+Math.round(n).toLocaleString('pt-BR');
const fd=s=>s.split('-').reverse().join('/');
let saved={};try{saved=JSON.parse(localStorage.getItem('voos')||'{}')}catch(e){}
$('up').textContent='Atualizado em '+D.atualizado;
D.aeroportos.forEach(a=>{const o=document.createElement('option');o.value=a;o.textContent=a;$('orig').appendChild(o)});
$('orig').value=D.aeroportos.includes(saved.orig)?saved.orig:D.aeroportos[0];
['',...D.companhias].forEach(c=>{const o=document.createElement('option');o.value=c;o.textContent=c||'Todas';$('cia').appendChild(o)});
$('cia').value=D.companhias.includes(saved.cia)?saved.cia:'';
$('de').value=saved.de||D.inicio;$('ate').value=saved.ate||D.fim;$('hot').checked=!!saved.hot;
function render(){
  const orig=$('orig').value,cia=$('cia').value,de=$('de').value,ate=$('ate').value,hot=$('hot').checked;
  try{localStorage.setItem('voos',JSON.stringify({orig,cia,de,ate,hot}))}catch(e){}
  const melhor={};
  D.voos.forEach(v=>{
    if(v.orig!==orig||(cia&&v.cia!==cia)||(de&&v.ida<de)||(ate&&v.volta>ate)||(hot&&!v.barato))return;
    if(!melhor[v.dest]||v.preco<melhor[v.dest].preco)melhor[v.dest]=v;
  });
  const lista=Object.values(melhor).sort((a,b)=>(b.barato-a.barato)||(a.preco-b.preco));
  const box=$('lista');box.textContent='';
  if(!lista.length){const p=document.createElement('p');p.textContent='Nenhum voo nessas datas.';box.appendChild(p);return}
  lista.forEach(v=>{
    const a=document.createElement('a');a.className='card'+(v.barato?' hot':'');
    if(v.link){a.href=v.link;a.rel='noopener'}
    const b=document.createElement('b');b.textContent=v.orig+' → '+v.dest;
    const p=document.createElement('span');p.className='p';p.textContent=fmt(v.preco);
    const s=document.createElement('small');s.textContent=fd(v.ida)+' → '+fd(v.volta)+(v.cia?' · '+v.cia:'')+(v.barato?' · 🔥 barato':'');
    a.append(b,p,s);box.appendChild(a);
  });
}
['orig','cia','de','ate','hot'].forEach(i=>$(i).addEventListener('change',render));
render();
</script></body></html>"""

def gerar_site():
    out = BASE / "docs"
    out.mkdir(exist_ok=True)
    e = html.escape
    voos = sorted(OFERTAS, key=lambda o: o["preco"])[:800]
    dados = {"aeroportos": AEROPORTOS, "companhias": sorted({v["cia"] for v in voos}), "inicio": INICIO, "fim": FIM,
             "atualizado": datetime.now().strftime("%d/%m/%Y %H:%M"), "voos": voos}
    dados_js = json.dumps(dados, ensure_ascii=False).replace("</", "<\\/")
    promos = "".join(f'<a class="card" href="{e(p["link"])}"><b>{e(p["titulo"])}</b></a>' for p in PROMOS) \
        or "<p>Nenhuma promoção recente.</p>"
    (out / "index.html").write_text(PAGINA.replace("__PROMOS__", promos).replace("__DATA__", dados_js), encoding="utf-8")
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
