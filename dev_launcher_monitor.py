import os
import json
import time
import threading
import requests
import psycopg2
import psycopg2.extras
from datetime import datetime, timezone
from flask import Flask, request, jsonify

# ══════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════
HELIUS_API_KEY  = os.environ.get("HELIUS_API_KEY", "")
TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT   = os.environ.get("TELEGRAM_CHAT", "")
DATABASE_URL    = os.environ.get("DATABASE_URL", "")
WEBHOOK_SECRET  = os.environ.get("WEBHOOK_SECRET", "")

DEV_WALLET      = "GpTXmkdvrTajqkzX1fBmC4BUjSboF9dHgfnqPqj8WAc4"
MC_THRESHOLD    = 10_000       # USD — filtro mínimo
TRIAGE_INTERVAL = 10           # segundos entre polls na triagem
TRIAGE_MAX_SECS = 120          # TTL do job de triagem (2 min)
TRIAGE_MAX_JOBS = 50           # jobs simultâneos máximos

# Checkpoints em segundos após cruzar 10k
CHECKPOINTS = {
    "t2":  2  * 60,
    "t5":  5  * 60,
    "t15": 15 * 60,
    "t60": 60 * 60,
}

# ── semáforo para limitar jobs de triagem simultâneos ──
_triage_sem = threading.Semaphore(TRIAGE_MAX_JOBS)

app = Flask(__name__)

# ══════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════
def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

# ══════════════════════════════════════════════════════════
# BANCO DE DADOS
# ══════════════════════════════════════════════════════════
def get_conn():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS tokens_dev (
                    id                      SERIAL PRIMARY KEY,
                    token_address           TEXT UNIQUE NOT NULL,
                    nome                    TEXT,
                    symbol                  TEXT,
                    detectado_em            TIMESTAMP,
                    criado_em               TIMESTAMP,
                    cruzou_10k_em           TIMESTAMP,
                    tempo_ate_10k_segundos  INTEGER,
                    mc_cross                NUMERIC,
                    status                  TEXT DEFAULT 'pendente'
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS snapshots_dev (
                    id              SERIAL PRIMARY KEY,
                    token_address   TEXT REFERENCES tokens_dev(token_address),
                    checkpoint      TEXT,
                    timestamp       TIMESTAMP,
                    mc              NUMERIC,
                    price           NUMERIC,
                    volume_5m       NUMERIC,
                    volume_1h       NUMERIC,
                    buys            INTEGER,
                    sells           INTEGER,
                    ratio_bs        NUMERIC,
                    holders         INTEGER,
                    liquidity       NUMERIC,
                    bc_progress     NUMERIC,
                    price_change_5m NUMERIC
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_td_address ON tokens_dev(token_address)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_sd_address ON snapshots_dev(token_address)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_td_status  ON tokens_dev(status)")
        conn.commit()
    log("✅ Banco inicializado")

def db_insert_token(token_address, nome, symbol, detectado_em, criado_em):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO tokens_dev (token_address, nome, symbol, detectado_em, criado_em, status)
                VALUES (%s, %s, %s, %s, %s, 'pendente')
                ON CONFLICT (token_address) DO NOTHING
            """, (token_address, nome, symbol, detectado_em, criado_em))
        conn.commit()

def db_update_status(token_address, status):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE tokens_dev SET status=%s WHERE token_address=%s",
                        (status, token_address))
        conn.commit()

def db_set_crossed(token_address, cruzou_em, tempo_seg, mc_cross):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE tokens_dev
                SET cruzou_10k_em=%s, tempo_ate_10k_segundos=%s, mc_cross=%s, status='monitorando'
                WHERE token_address=%s
            """, (cruzou_em, tempo_seg, mc_cross, token_address))
        conn.commit()

def db_save_snapshot(token_address, checkpoint, ts, data):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO snapshots_dev
                    (token_address, checkpoint, timestamp, mc, price,
                     volume_5m, volume_1h, buys, sells, ratio_bs,
                     holders, liquidity, bc_progress, price_change_5m)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                token_address, checkpoint, ts,
                data.get("mc"), data.get("price"),
                data.get("volume_5m"), data.get("volume_1h"),
                data.get("buys"), data.get("sells"), data.get("ratio_bs"),
                data.get("holders"), data.get("liquidity"),
                data.get("bc_progress"), data.get("price_change_5m"),
            ))
        conn.commit()

def db_get_token(token_address):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM tokens_dev WHERE token_address=%s", (token_address,))
            return cur.fetchone()

# ══════════════════════════════════════════════════════════
# DEXSCREENER
# ══════════════════════════════════════════════════════════
_DEX_BASE = "https://api.dexscreener.com/latest/dex/tokens"

def fetch_dexscreener(token_address):
    """Retorna dict com métricas ou None se não encontrado/erro."""
    try:
        r = requests.get(f"{_DEX_BASE}/{token_address}", timeout=10)
        if r.status_code != 200:
            return None
        data = r.json()
        pairs = data.get("pairs") or []
        if not pairs:
            return None
        # Prefere par Solana com maior liquidez
        sol_pairs = [p for p in pairs if p.get("chainId") == "solana"]
        pair = max(sol_pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0)) if sol_pairs else pairs[0]

        mc = float(pair.get("marketCap") or pair.get("fdv") or 0)
        price = float(pair.get("priceUsd") or 0)
        liq = float((pair.get("liquidity") or {}).get("usd") or 0)
        vol5m = float((pair.get("volume") or {}).get("m5") or 0)
        vol1h = float((pair.get("volume") or {}).get("h1") or 0)
        txns5m = pair.get("txns", {}).get("m5", {})
        buys  = int(txns5m.get("buys") or 0)
        sells = int(txns5m.get("sells") or 0)
        ratio = round(buys / (buys + sells), 4) if (buys + sells) > 0 else 0
        pc5m  = float((pair.get("priceChange") or {}).get("m5") or 0)

        # Bonding curve progress (Pump.fun embeds it in dexscreener sometimes)
        bc = None
        info = pair.get("info") or {}
        for ext in info.get("extensions") or []:
            if ext.get("label", "").lower() in ("bonding curve", "bondingcurve"):
                try:
                    bc = float(ext["value"])
                except Exception:
                    pass

        return {
            "mc": mc,
            "price": price,
            "liquidity": liq,
            "volume_5m": vol5m,
            "volume_1h": vol1h,
            "buys": buys,
            "sells": sells,
            "ratio_bs": ratio,
            "price_change_5m": pc5m,
            "bc_progress": bc,
        }
    except Exception as e:
        log(f"DexScreener erro [{token_address[:8]}]: {e}")
        return None

# ══════════════════════════════════════════════════════════
# HELIUS RPC — holders
# ══════════════════════════════════════════════════════════
def fetch_holders(token_address):
    """Retorna número de holders via Helius getTokenAccounts."""
    if not HELIUS_API_KEY:
        return None
    url = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getTokenAccounts",
        "params": {
            "mint": token_address,
            "limit": 1000,
            "options": {"showZeroBalance": False},
        },
    }
    try:
        r = requests.post(url, json=payload, timeout=15)
        result = r.json().get("result") or {}
        accounts = result.get("token_accounts") or []
        return len(accounts)
    except Exception as e:
        log(f"Helius holders erro [{token_address[:8]}]: {e}")
        return None

# ══════════════════════════════════════════════════════════
# TELEGRAM
# ══════════════════════════════════════════════════════════
def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT,
            "text": msg,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=10)
    except Exception as e:
        log(f"Telegram erro: {e}")

def alerta_qualificado(token_address, symbol, nome, tempo_seg, dex_data, holders):
    mc_k  = round((dex_data.get("mc") or 0) / 1000, 1)
    liq_k = round((dex_data.get("liquidity") or 0) / 1000, 1)
    ratio = round((dex_data.get("ratio_bs") or 0) * 100, 1)
    bc    = dex_data.get("bc_progress")
    bc_str = f"{bc:.1f}%" if bc is not None else "N/A"
    h_str  = str(holders) if holders else "N/A"

    msg = (
        "🚨 <b>DEV LAUNCHER — Novo Token Qualificado</b>\n\n"
        f"🪙 <b>Nome:</b> {nome} ({symbol})\n"
        f"📍 <b>Endereço:</b> <code>{token_address}</code>\n"
        f"⏱️ <b>Tempo até 10k:</b> {tempo_seg}s desde lançamento\n"
        f"💰 <b>MC atual:</b> ${mc_k}k\n"
        f"📊 <b>Ratio B/S:</b> {ratio}%\n"
        f"💧 <b>Liquidez:</b> ${liq_k}k\n"
        f"👥 <b>Holders:</b> {h_str}\n"
        f"📈 <b>BC Progress:</b> {bc_str}\n"
        f"🔗 <a href='https://dexscreener.com/solana/{token_address}'>DexScreener</a>  "
        f"🔗 <a href='https://pump.fun/{token_address}'>Pump.fun</a>"
    )
    send_telegram(msg)

# ══════════════════════════════════════════════════════════
# COLETA DE SNAPSHOT COMPLETO
# ══════════════════════════════════════════════════════════
def coletar_snapshot(token_address, checkpoint):
    dex = fetch_dexscreener(token_address)
    if not dex:
        log(f"⚠️  Snapshot {checkpoint} sem dados DexScreener [{token_address[:8]}]")
        return None
    holders = fetch_holders(token_address)
    dex["holders"] = holders
    ts = datetime.now(timezone.utc)
    db_save_snapshot(token_address, checkpoint, ts, dex)
    log(f"📸 Snapshot {checkpoint} salvo [{token_address[:8]}] MC=${dex['mc']:,.0f}")
    return dex

# ══════════════════════════════════════════════════════════
# SCHEDULER DE CHECKPOINTS
# ══════════════════════════════════════════════════════════
def agendar_checkpoints(token_address, cruzou_em):
    """Agenda coleta nos checkpoints T+2, T+5, T+15, T+60."""
    cruzou_ts = cruzou_em.timestamp()

    def rodar_checkpoint(nome_cp, delay_seg):
        agora = time.time()
        espera = cruzou_ts + delay_seg - agora
        if espera > 0:
            time.sleep(espera)
        log(f"⏰ Executando checkpoint {nome_cp} [{token_address[:8]}]")
        coletar_snapshot(token_address, nome_cp)

        # Após T+60 finaliza o token
        if nome_cp == "t60":
            finalizar_token(token_address)

    for nome, secs in CHECKPOINTS.items():
        t = threading.Thread(
            target=rodar_checkpoint,
            args=(nome, secs),
            daemon=True,
            name=f"cp-{nome}-{token_address[:8]}"
        )
        t.start()

def finalizar_token(token_address):
    """Calcula métricas finais e atualiza status."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT mc, checkpoint FROM snapshots_dev
                WHERE token_address=%s ORDER BY timestamp
            """, (token_address,))
            rows = cur.fetchall()
            cur.execute("SELECT mc_cross FROM tokens_dev WHERE token_address=%s", (token_address,))
            tok = cur.fetchone()

    if not rows or not tok:
        db_update_status(token_address, "concluido")
        return

    mc_cross = float(tok["mc_cross"] or 0)
    mcs = [float(r["mc"] or 0) for r in rows if r["mc"]]
    pico_mc = max(mcs) if mcs else mc_cross

    if mc_cross > 0:
        var_pico = ((pico_mc - mc_cross) / mc_cross) * 100
    else:
        var_pico = 0

    # Pega MC do último snapshot (t60)
    mc_final = mcs[-1] if mcs else mc_cross
    var_final = ((mc_final - mc_cross) / mc_cross) * 100 if mc_cross > 0 else 0

    if var_pico > 200 and var_final > 100:
        categoria = "🏆 VENCEDOR"
    elif var_pico > 50 and var_final < 0:
        categoria = "🎯 PUMP & DUMP"
    elif var_pico > 50 and var_final > 20:
        categoria = "📈 BOM TRADE"
    else:
        categoria = "💀 MORREU"

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE tokens_dev SET status='concluido' WHERE token_address=%s
            """, (token_address,))
        conn.commit()

    log(f"🏁 Token finalizado [{token_address[:8]}]: {categoria} | pico={pico_mc:,.0f} var_pico={var_pico:.1f}%")
    msg_final = (
        f"🏁 <b>Token Finalizado</b>\n"
        f"<code>{token_address}</code>\n"
        f"Categoria: {categoria}\n"
        f"MC Cross: ${mc_cross:,.0f} | Pico: ${pico_mc:,.0f}\n"
        f"Var Pico: {var_pico:.1f}% | Var Final: {var_final:.1f}%"
    )
    send_telegram(msg_final)

# ══════════════════════════════════════════════════════════
# JOB DE TRIAGEM (polling 2 minutos)
# ══════════════════════════════════════════════════════════
def job_triagem(token_address, detectado_em, criado_em):
    """Polling a cada 10s por 2min. Se cruzar 10k → ativa monitoramento."""
    if not _triage_sem.acquire(blocking=False):
        log(f"⚠️  Semáforo cheio, token {token_address[:8]} descartado na fila")
        db_update_status(token_address, "descartado")
        return

    try:
        deadline = time.time() + TRIAGE_MAX_SECS
        tentativas = 0

        while time.time() < deadline:
            tentativas += 1
            dex = fetch_dexscreener(token_address)

            if dex and dex["mc"] >= MC_THRESHOLD:
                agora = datetime.now(timezone.utc)

                # Calcula tempo desde criação do token
                if isinstance(criado_em, datetime):
                    tempo_seg = int((agora - criado_em.replace(tzinfo=timezone.utc)
                                     if criado_em.tzinfo is None else agora - criado_em).total_seconds())
                else:
                    tempo_seg = int((agora - detectado_em).total_seconds())

                log(f"🚀 Token qualificado [{token_address[:8]}] MC=${dex['mc']:,.0f} em {tempo_seg}s")
                db_set_crossed(token_address, agora, tempo_seg, dex["mc"])

                # Snapshot inicial no cruzamento (checkpoint 'cross')
                holders = fetch_holders(token_address)
                dex["holders"] = holders
                db_save_snapshot(token_address, "cross", agora, dex)

                # Alerta Telegram
                tok = db_get_token(token_address)
                nome   = (tok.get("nome")   or "Desconhecido") if tok else "Desconhecido"
                symbol = (tok.get("symbol") or "???")          if tok else "???"
                alerta_qualificado(token_address, symbol, nome, tempo_seg, dex, holders)

                # Agenda checkpoints futuros
                agendar_checkpoints(token_address, agora)
                return

            log(f"🔍 Triagem [{tentativas}] [{token_address[:8]}] MC=${dex['mc'] if dex else 0:,.0f}")
            time.sleep(TRIAGE_INTERVAL)

        # 2 minutos sem cruzar → descarta
        log(f"🗑️  Descartado [{token_address[:8]}] — não cruzou {MC_THRESHOLD:,} em {TRIAGE_MAX_SECS}s")
        db_update_status(token_address, "descartado")

    finally:
        _triage_sem.release()

# ══════════════════════════════════════════════════════════
# HELIUS WEBHOOK
# ══════════════════════════════════════════════════════════
def extrair_mint_token(payload_list):
    """
    Retorna (token_address, nome, symbol, criado_em) a partir do payload Helius.
    Suporta os formatos enhanced e raw da API Helius.
    """
    for tx in (payload_list if isinstance(payload_list, list) else [payload_list]):
        # Filtro por fee payer = carteira monitorada
        fee_payer = tx.get("feePayer", "")
        if fee_payer != DEV_WALLET:
            # Verifica também em accountData
            account_data = tx.get("accountData") or []
            payers = [a.get("account") for a in account_data if a.get("nativeBalanceChange", 0) < 0]
            if DEV_WALLET not in payers:
                continue

        # Tenta extrair via tokenTransfers (enhanced)
        for tt in (tx.get("tokenTransfers") or []):
            mint = tt.get("mint", "")
            if mint and mint != "So11111111111111111111111111111111111111112":
                return mint, tt.get("tokenName", ""), tt.get("tokenSymbol", ""), _ts(tx)

        # Tenta via instructions (raw)
        for ix in (tx.get("instructions") or []):
            if ix.get("programId") in (
                "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
            ):
                accounts = ix.get("accounts") or []
                if accounts:
                    return accounts[0], "", "", _ts(tx)

        # Tenta via description (fallback)
        desc = tx.get("description", "")
        if "created" in desc.lower() or "minted" in desc.lower():
            for td in (tx.get("tokenTransfers") or []):
                if td.get("mint"):
                    return td["mint"], "", "", _ts(tx)

    return None, None, None, None

def _ts(tx):
    ts_unix = tx.get("timestamp")
    if ts_unix:
        return datetime.fromtimestamp(ts_unix, tz=timezone.utc)
    return datetime.now(timezone.utc)

@app.route("/webhook", methods=["POST"])
def webhook():
    # Verificação opcional de secret
    if WEBHOOK_SECRET:
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {WEBHOOK_SECRET}":
            return jsonify({"error": "unauthorized"}), 401

    try:
        payload = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "invalid json"}), 400

    if not payload:
        return jsonify({"ok": True})

    token_address, nome, symbol, criado_em = extrair_mint_token(payload)

    if not token_address:
        return jsonify({"ok": True, "msg": "no relevant mint found"})

    log(f"📨 Webhook recebido: {token_address[:8]} ({symbol})")

    detectado_em = datetime.now(timezone.utc)
    db_insert_token(token_address, nome or "", symbol or "", detectado_em, criado_em)

    t = threading.Thread(
        target=job_triagem,
        args=(token_address, detectado_em, criado_em),
        daemon=True,
        name=f"triage-{token_address[:8]}"
    )
    t.start()

    return jsonify({"ok": True, "token": token_address})

# ══════════════════════════════════════════════════════════
# DASHBOARD SIMPLES
# ══════════════════════════════════════════════════════════
@app.route("/status")
def status():
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT status, COUNT(*) as total
                    FROM tokens_dev GROUP BY status
                """)
                contagens = {r["status"]: r["total"] for r in cur.fetchall()}
                cur.execute("""
                    SELECT t.token_address, t.symbol, t.status,
                           t.cruzou_10k_em, t.mc_cross, t.tempo_ate_10k_segundos
                    FROM tokens_dev t
                    WHERE t.status IN ('monitorando','concluido')
                    ORDER BY t.detectado_em DESC LIMIT 20
                """)
                recentes = [dict(r) for r in cur.fetchall()]
                for r in recentes:
                    if r.get("cruzou_10k_em"):
                        r["cruzou_10k_em"] = r["cruzou_10k_em"].isoformat()
        return jsonify({"contagens": contagens, "recentes": recentes})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/tokens")
def tokens():
    limit = min(int(request.args.get("limit", 50)), 200)
    status_filter = request.args.get("status", "")
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                if status_filter:
                    cur.execute("""
                        SELECT * FROM tokens_dev WHERE status=%s
                        ORDER BY detectado_em DESC LIMIT %s
                    """, (status_filter, limit))
                else:
                    cur.execute("""
                        SELECT * FROM tokens_dev
                        ORDER BY detectado_em DESC LIMIT %s
                    """, (limit,))
                rows = cur.fetchall()
        result = []
        for r in rows:
            d = dict(r)
            for k in ("detectado_em", "criado_em", "cruzou_10k_em"):
                if d.get(k):
                    d[k] = d[k].isoformat()
            result.append(d)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/snapshots/<token_address>")
def snapshots(token_address):
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT * FROM snapshots_dev WHERE token_address=%s ORDER BY timestamp
                """, (token_address,))
                rows = [dict(r) for r in cur.fetchall()]
        for r in rows:
            if r.get("timestamp"):
                r["timestamp"] = r["timestamp"].isoformat()
        return jsonify(rows)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/health")
def health():
    return jsonify({"status": "ok", "ts": datetime.now(timezone.utc).isoformat()})

# ══════════════════════════════════════════════════════════
# STARTUP — inicializa banco ao carregar o módulo (gunicorn)
# ══════════════════════════════════════════════════════════
if DATABASE_URL:
    init_db()
else:
    log("⚠️  DATABASE_URL não configurado — banco não inicializado")

# ══════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    log(f"🚀 dev-launcher-monitor iniciando na porta {port}")
    app.run(host="0.0.0.0", port=port)
