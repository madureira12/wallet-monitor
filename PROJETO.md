# dev-launcher-monitor — Documentação Completa

## Objetivo
Monitorar carteira Solana que lança tokens em massa (via Pump.fun) para identificar os ~2% que disparam.

**Carteira monitorada:** `GpTXmkdvrTajqkzX1fBmC4BUjSboF9dHgfnqPqj8WAc4`

---

## Infraestrutura

| Componente | Serviço | Detalhe |
|---|---|---|
| App / Worker | Render | https://dev-launcher-monitor.onrender.com |
| Banco de dados | Railway | PostgreSQL — URL pública (maglev.proxy.rlwy.net) |
| Webhook | Helius | Nova conta — carteira GpTXm... tipo CREATE |
| GitHub | madureira12/wallet-monitor | branch main |
| Telegram | Desativado por ora | Ativar depois que estiver estável |

---

## Arquivos do Projeto

| Arquivo | Descrição |
|---|---|
| `dev_launcher_monitor.py` | App principal (Flask + jobs de triagem + checkpoints) |
| `requirements.txt` | requests, flask, psycopg2-binary, gunicorn |
| `render.yaml` | Config de deploy no Render |
| `schema_dev_launcher.sql` | Schema PostgreSQL + view tokens_dev_performance |

---

## Fluxo do Sistema

```
Helius Webhook (nova mint da carteira GpTXm...)
    ↓
db_insert_token() → status: pendente
    ↓
job_triagem() em thread separada (semáforo: max 50 simultâneos)
    ↓
Polling DexScreener a cada 10s por até 2min (12 tentativas)
    ↓
MC < 10k em 2min? → status: descartado (90-98% dos tokens)
MC ≥ 10k?         → status: monitorando
    ↓
Snapshot 'cross' salvo + Telegram alert (quando ativado)
    ↓
Checkpoints agendados em threads:
  - t2:  2min após cruzar 10k
  - t5:  5min após cruzar 10k
  - t15: 15min após cruzar 10k
  - t60: 60min após cruzar 10k → finaliza token
    ↓
Classificação final:
  🏆 VENCEDOR     — pico > 200% e final > 100%
  🎯 PUMP & DUMP  — pico > 50% e colapsou
  📈 BOM TRADE    — pico > 50% e final > 20%
  💀 MORREU       — pico < 20%
```

---

## Dados Capturados por Snapshot

| Campo | Fonte |
|---|---|
| mc, price | DexScreener |
| volume_5m, volume_1h | DexScreener |
| buys, sells, ratio_bs | DexScreener |
| liquidity, bc_progress | DexScreener |
| price_change_5m | DexScreener |
| holders | Helius RPC (getTokenAccounts) |

---

## Banco de Dados

### Tabela tokens_dev
```sql
id, token_address, nome, symbol,
detectado_em,          -- quando webhook recebeu
criado_em,             -- timestamp do bloco
cruzou_10k_em,         -- momento que atingiu 10k MC
tempo_ate_10k_segundos,
mc_cross,              -- MC no momento do cruzamento
status                 -- pendente | monitorando | concluido | descartado
```

### Tabela snapshots_dev
```sql
id, token_address, checkpoint, timestamp,
mc, price, volume_5m, volume_1h,
buys, sells, ratio_bs,
holders, liquidity, bc_progress, price_change_5m
```

### View tokens_dev_performance
Calcula automaticamente: var_t2, var_t5, var_t15, var_t60, pico_mc, var_pico, categoria_final

---

## Endpoints

| Endpoint | Descrição |
|---|---|
| `POST /webhook` | Recebe payload do Helius |
| `GET /health` | Health check |
| `GET /status` | Contagem por status + 20 tokens recentes |
| `GET /tokens?status=monitorando&limit=50` | Lista tokens |
| `GET /snapshots/<token_address>` | Snapshots de um token |

---

## Configuração Render (variáveis de ambiente)

| Variável | Status |
|---|---|
| `DATABASE_URL` | ✅ Configurado (Railway public URL) |
| `HELIUS_API_KEY` | ✅ Configurado |
| `TELEGRAM_TOKEN` | ⏸️ Deixado vazio por ora |
| `TELEGRAM_CHAT` | ⏸️ Deixado vazio por ora |
| `WEBHOOK_SECRET` | Opcional — não configurado |

---

## Helius Webhook

- **URL:** `https://dev-launcher-monitor.onrender.com/webhook`
- **Account:** `GpTXmkdvrTajqkzX1fBmC4BUjSboF9dHgfnqPqj8WAc4`
- **Transaction Types:** `CREATE`
- **Webhook Type:** Enhanced

---

## Distribuição Esperada dos Tokens

- 90% morrem antes de 1 minuto → descartados na triagem
- 8% morrem antes de 20k MC → descartados ou MORREU
- 2% disparam → 80k a 500k MC → VENCEDOR / BOM TRADE

---

## Próximos Passos

1. **Aguardar dados** — ~50 a 100 tokens qualificados (cruzaram 10k)
2. **Analisar padrões** — ratio_bs, bc_progress, tempo_ate_10k, holders no cross
3. **Montar score de entrada** — filtrar tokens com maior probabilidade de disparar
4. **Fase 2 (futuro)** — automatizar compra quando score alto

---

## Separação dos Projetos

| | wallet-monitor (base) | dev-launcher-monitor (novo) |
|---|---|---|
| GitHub | justafake33/wallet-monitor | madureira12/wallet-monitor |
| Railway | projeto existente | novo projeto |
| Render | worker existente | dev-launcher-monitor.onrender.com |
| Helius | conta atual | nova conta |
| Objetivo | 4 carteiras, score de compra | 1 carteira, análise de lançamentos |

---

## Estratégia (análise sem dados ainda)

O dev tem winrate ~85% nas vendas iniciais — ele sabe quais vão bombar pois lançou.

**Fases:**
1. Coletar dados (agora)
2. Com ~100 tokens: identificar padrões dos vencedores nos primeiros minutos
3. Automatizar entrada quando critérios forem atendidos

**Variáveis a analisar quando tivermos dados:**
- `ratio_bs` no cross — tokens com mais compras que vendas tendem a continuar
- `tempo_ate_10k` — tokens rápidos (< 30s) performam melhor?
- `bc_progress` no cruzamento — qual % é ideal?
- `volume_5m` mínimo para não ser pump & dump
- `holders` no cross — distribuição saudável
