# Voos bot

1. Token grátis de dados de voos: criar conta em travelpayouts.com (Data API).
2. Telegram: falar com @BotFather → /newbot → copiar o token. Mandar um "oi" pro seu bot e abrir
   https://api.telegram.org/bot<TOKEN>/getUpdates para ver o seu chat_id.
3. `cp config.example.json config.json` e preencher.
4. Testar: `python3 voos_bot.py --dry` (imprime em vez de enviar).
5. Agendar a cada 6h: `0 */6 * * * cd /caminho/voos-bot && python3 voos_bot.py`

Só usa a biblioteca padrão do Python 3.8+. Aeroporto de saída = "aeroporto_saida" no config.
Primeiros dias: sem histórico, só avisa o que estiver abaixo do teto; depois passa a comparar com a mediana.

## Rodar de graça no GitHub Actions + app no celular
1. Crie um repositório no GitHub e suba todo o conteúdo desta pasta (inclusive `.github/`).
   Para o app (GitHub Pages) funcionar no plano grátis, o repositório precisa ser **público**.
   Isso é seguro: tokens ficam nos Secrets; só o `config.json` (aeroporto, tetos) fica visível.
2. Settings → Secrets and variables → Actions: crie `TRAVELPAYOUTS_TOKEN`, `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`.
3. Settings → Actions → General → Workflow permissions: marque "Read and write permissions".
4. Aba Actions → voos-bot → Run workflow (primeira execução, cria a pasta `docs/`).
5. Settings → Pages → Source: "Deploy from a branch" → main → /docs. O link do app aparece ali.
6. No celular, abra o link e use "Adicionar à tela inicial": vira um app. Os avisos chegam pelo Telegram.
O histórico de preços fica no cache do Actions entre as execuções.
