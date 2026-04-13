"""
X-SHOT — Scanner + Live Dashboard in ONE
Runs on Railway.app. One URL gives you the full dashboard.
Scanner runs in background, sends Telegram alerts.
Dashboard shows live prices, signals, indicators — from any device, any time.
"""
import requests, time, os, json, html
from datetime import datetime, timezone, timedelta
from threading import Thread, Lock
from http.server import HTTPServer, BaseHTTPRequestHandler

# ==================== CONFIG ====================
PORT = int(os.environ.get('PORT', 8080))
TG_TOKEN = os.environ.get('TG_TOKEN', '')
TG_CHAT = os.environ.get('TG_CHAT_ID', '')
SCAN_INTERVAL = 180

COINS = [
    ('BTCUSDT','BTC','Bitcoin'), ('ETHUSDT','ETH','Ethereum'),
    ('SOLUSDT','SOL','Solana'), ('XRPUSDT','XRP','Ripple'),
    ('ADAUSDT','ADA','Cardano'), ('AVAXUSDT','AVAX','Avalanche'),
    ('LINKUSDT','LINK','Chainlink'), ('DOTUSDT','DOT','Polkadot'),
    ('MATICUSDT','MATIC','Polygon'), ('ATOMUSDT','ATOM','Cosmos'),
    ('ALGOUSDT','ALGO','Algorand'), ('FILUSDT','FIL','Filecoin'),
    ('HBARUSDT','HBAR','Hedera'), ('VETUSDT','VET','VeChain'),
    ('NEARUSDT','NEAR','NEAR'),
]

sent_alerts = {}
COOLDOWN = 1800
data_lock = Lock()

# Shared state — dashboard reads this
STATE = {
    'last_scan': 'starting...',
    'total_alerts': 0,
    'coins': {},       # {sym: {price, pct, rsi, macd, net, signal, ...}}
    'signals': [],     # [{sym, type, price, pct, reasons, tgt, stp, rr, net}]
    'scan_count': 0,
}

# ==================== TELEGRAM ====================
def tg(msg):
    if not TG_TOKEN or not TG_CHAT: return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id":TG_CHAT,"text":msg}, timeout=10)
    except Exception as e: print(f"  TG err: {e}")

# ==================== BINANCE ====================
def get_candles(sym, interval='1h', limit=60):
    try:
        r = requests.get("https://api.binance.com/api/v3/klines",
            params={"symbol":sym,"interval":interval,"limit":limit}, timeout=10)
        return r.json()
    except: return []

def get_ticker(sym):
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/24hr",
            params={"symbol":sym}, timeout=10)
        return r.json()
    except: return {}

# ==================== TECHNICAL ANALYSIS ====================
def ema(c, p):
    r=[c[0]]; m=2/(p+1)
    for i in range(1,len(c)): r.append((c[i]-r[-1])*m+r[-1])
    return r

def calc_sma(c, p):
    r=[]
    for i in range(len(c)):
        if i<p-1: r.append(None)
        else: r.append(sum(c[i-p+1:i+1])/p)
    return r

def calc_rsi(c, p=14):
    if len(c)<p+1: return 50
    g,l=0,0
    for i in range(1,p+1):
        d=c[i]-c[i-1]
        if d>0: g+=d
        else: l+=abs(d)
    ag,al=g/p,l/p
    for i in range(p+1,len(c)):
        d=c[i]-c[i-1]
        ag=(ag*(p-1)+max(d,0))/p
        al=(al*(p-1)+max(-d,0))/p
    return 100 if al==0 else 100-(100/(1+ag/al))

def calc_macd_hist(c):
    e12,e26=ema(c,12),ema(c,26)
    ml=[e12[i]-e26[i] for i in range(len(c))]
    sl=ema(ml,9)
    return [ml[i]-sl[i] for i in range(len(ml))]

def calc_bollinger(c, p=20):
    if len(c)<p: return None,None,None
    s=sum(c[-p:])/p
    v=sum((x-s)**2 for x in c[-p:])/p
    return s+2*v**0.5, s, s-2*v**0.5

def calc_stoch(klines, p=14):
    if len(klines)<p: return 50
    highs=[float(k[2]) for k in klines[-p:]]
    lows=[float(k[3]) for k in klines[-p:]]
    close=float(klines[-1][4])
    hh,ll=max(highs),min(lows)
    return ((close-ll)/(hh-ll)*100) if hh!=ll else 50

def calc_atr(klines, p=14):
    trs=[]
    for i in range(1,len(klines)):
        h,l,pc=float(klines[i][2]),float(klines[i][3]),float(klines[i-1][4])
        trs.append(max(h-l,abs(h-pc),abs(l-pc)))
    return sum(trs[-p:])/min(len(trs),p) if trs else 0

def detect_pats(klines):
    bull,bear=0,0
    for i in range(max(0,len(klines)-5),len(klines)):
        o,h,l,c=float(klines[i][1]),float(klines[i][2]),float(klines[i][3]),float(klines[i][4])
        body,rng=abs(c-o),h-l
        if rng==0: continue
        ub,lb=h-max(o,c),min(o,c)-l
        if lb>body*2 and ub<body*0.5 and c>o: bull+=1
        if ub>body*2 and lb<body*0.5 and o>c: bear+=1
        if i>0:
            po,pc2=float(klines[i-1][1]),float(klines[i-1][4])
            pb=abs(pc2-po)
            if pc2<po and c>o and body>pb: bull+=1
            if pc2>po and c<o and body>pb: bear+=1
    return bull,bear

# ==================== SCANNER ====================
def scan():
    global STATE
    jeddah=datetime.now(timezone(timedelta(hours=3)))
    scan_time=jeddah.strftime('%H:%M:%S AST')
    print(f"\n{'='*45}\n  X-SHOT — {scan_time}\n{'='*45}")

    coins_data={}
    signals=[]
    alerts=[]

    for sym,name,fullname in COINS:
        try:
            kl=get_candles(sym)
            if len(kl)<30: continue
            tk=get_ticker(sym)
            closes=[float(k[4]) for k in kl]
            price=closes[-1]
            pct=float(tk.get('priceChangePercent',0))
            vol=float(tk.get('quoteVolume',0))

            r=calc_rsi(closes); h=calc_macd_hist(closes)
            bbu,bbm,bbl=calc_bollinger(closes)
            st=calc_stoch(kl); at=calc_atr(kl)
            e20=ema(closes,20)[-1]
            s50=[x for x in calc_sma(closes,50) if x is not None]
            s50v=s50[-1] if s50 else price
            bp,brp=detect_pats(kl)

            bull,bear=0,0; reasons=[]

            if r<25: bull+=3; reasons.append(f"RSI {r:.0f} deep OS")
            elif r<35: bull+=2; reasons.append(f"RSI {r:.0f} OS")
            elif r>75: bear+=3; reasons.append(f"RSI {r:.0f} deep OB")
            elif r>65: bear+=2; reasons.append(f"RSI {r:.0f} OB")

            if len(h)>=3:
                if h[-1]>0 and h[-2]<=0 and h[-3]<=0: bull+=2; reasons.append("MACD bull cross")
                elif h[-1]<0 and h[-2]>=0 and h[-3]>=0: bear+=2; reasons.append("MACD bear cross")
                if h[-1]>0 and h[-1]>h[-2]: bull+=1; reasons.append("MACD rising")
                elif h[-1]<0 and h[-1]<h[-2]: bear+=1; reasons.append("MACD falling")

            if price>e20 and e20>s50v: bull+=2; reasons.append("Bullish stack")
            elif price<e20 and e20<s50v: bear+=2; reasons.append("Bearish stack")
            elif price>e20: bull+=1
            else: bear+=1

            if bbl and price<=bbl*1.002: bull+=2; reasons.append("Lower BB")
            elif bbu and price>=bbu*0.998: bear+=2; reasons.append("Upper BB")

            if st<15: bull+=2; reasons.append(f"Stoch {st:.0f}")
            elif st<25: bull+=1
            elif st>85: bear+=2; reasons.append(f"Stoch {st:.0f}")
            elif st>75: bear+=1

            if bp>=2: bull+=2
            elif bp==1: bull+=1
            if brp>=2: bear+=2
            elif brp==1: bear+=1

            net=bull-bear
            sig='—'
            if net>=5: sig='BUY'
            elif net<=-5: sig='SELL'
            elif net>=3: sig='WATCH'
            elif net<=-3: sig='WATCH'

            coins_data[name]={
                'sym':sym,'name':name,'fullname':fullname,
                'price':price,'pct':pct,'vol':vol,
                'rsi':r,'macd_h':h[-1] if h else 0,'stoch':st,'atr':at,
                'ema20':e20,'sma50':s50v,'bbu':bbu,'bbl':bbl,
                'bull':bull,'bear':bear,'net':net,'signal':sig,
                'reasons':reasons[:5]
            }

            if sig in ('BUY','SELL') and abs(net)>=5:
                tgt=price+at*3 if sig=='BUY' else price-at*3
                stp=price-at*1.5 if sig=='BUY' else price+at*1.5
                rr=abs(tgt-price)/abs(stp-price) if abs(stp-price)>0 else 0
                signals.append({'name':name,'fullname':fullname,'type':sig,'price':price,'pct':pct,
                    'reasons':reasons[:4],'tgt':tgt,'stp':stp,'rr':rr,'net':net})

                # Telegram with cooldown
                now=time.time()
                prev=sent_alerts.get(name)
                if not(prev and prev['type']==sig and (now-prev['time'])<COOLDOWN):
                    alerts.append(signals[-1])
                    sent_alerts[name]={'type':sig,'time':now}

            print(f"  {'🟢' if sig=='BUY' else '🔴' if sig=='SELL' else '🟡' if sig=='WATCH' else '➡️'}  {name} ${price:,.2f} net={net} {sig}")
            time.sleep(0.12)
        except Exception as e:
            print(f"  ❌ {name}: {e}")

    with data_lock:
        STATE['last_scan']=scan_time
        STATE['coins']=coins_data
        STATE['signals']=signals
        STATE['scan_count']+=1

    if alerts:
        STATE['total_alerts']+=len(alerts)
        msg=f"⚡ X-SHOT Alert\n{jeddah.strftime('%H:%M')} AST\n\n"
        for a in alerts:
            e='🟢' if a['type']=='BUY' else '🔴'
            msg+=f"{e} {a['type']} {a['name']} ${a['price']:,.2f} ({a['pct']:+.1f}%)\n"
            msg+=f"  {' · '.join(a['reasons'])}\n"
            msg+=f"  Target ${a['tgt']:,.2f} | Stop ${a['stp']:,.2f} | R:R {a['rr']:.1f}\n"
            msg+=f"  Strength: {abs(a['net'])}/12\n\n"
        msg+="✅ Halal · Not financial advice"
        tg(msg)
        print(f"\n  📨 Sent {len(alerts)} alerts")
    else:
        print(f"\n  😴 No new alerts")

def scanner_loop():
    while True:
        try: scan()
        except Exception as e: print(f"Scan error: {e}")
        time.sleep(SCAN_INTERVAL)

# ==================== WEB DASHBOARD ====================
def build_dashboard():
    with data_lock:
        s = STATE.copy()
        coins = s.get('coins', {})
        signals = s.get('signals', [])

    now_str = datetime.now(timezone(timedelta(hours=3))).strftime('%H:%M:%S AST')

    # Build coin rows
    coin_rows = ''
    for sym,name,fullname in COINS:
        c = coins.get(name)
        if not c:
            coin_rows += f'<tr><td class="tk">{name}</td><td>{fullname}</td><td>—</td><td>—</td><td>—</td><td>—</td><td>—</td></tr>'
            continue
        pct_cls = 'pos' if c['pct']>=0 else 'neg'
        sig_cls = 'buy' if c['signal']=='BUY' else 'sell' if c['signal']=='SELL' else 'watch' if c['signal']=='WATCH' else ''
        p = c['price']
        pf = f"${p:,.0f}" if p>100 else f"${p:,.2f}" if p>1 else f"${p:,.4f}"
        vol_m = f"{c['vol']/1e6:,.0f}M"
        coin_rows += f'''<tr>
            <td class="tk">{name}</td><td class="nm">{fullname}</td>
            <td class="mono">{pf}</td>
            <td class="mono {pct_cls}">{c['pct']:+.2f}%</td>
            <td class="mono">RSI {c['rsi']:.0f}</td>
            <td class="mono sm">{vol_m}</td>
            <td><span class="sig-badge {sig_cls}">{c['signal']}</span></td>
        </tr>'''

    # Build signal cards
    sig_html = ''
    if signals:
        for sg in sorted(signals, key=lambda x: abs(x['net']), reverse=True):
            emoji = '🟢' if sg['type']=='BUY' else '🔴'
            p = sg['price']
            pf = f"${p:,.0f}" if p>100 else f"${p:,.2f}"
            tf = f"${sg['tgt']:,.0f}" if sg['tgt']>100 else f"${sg['tgt']:,.2f}"
            sf = f"${sg['stp']:,.0f}" if sg['stp']>100 else f"${sg['stp']:,.2f}"
            bar = '█' * min(abs(sg['net']),12) + '░' * max(12-abs(sg['net']),0)
            sig_html += f'''<div class="sig-card">
                <div class="sig-top"><span>{emoji} <b class="tk">{sg['name']}</b> {sg['fullname']}</span>
                <span class="sig-badge {'buy' if sg['type']=='BUY' else 'sell'}">{sg['type']}</span></div>
                <div class="sig-bar">{bar} {abs(sg['net'])}/12</div>
                <div class="sig-reasons">{' · '.join(sg['reasons'])}</div>
                <div class="sig-levels">Entry {pf} → Target {tf} | Stop {sf} | R:R {sg['rr']:.1f}</div>
            </div>'''
    else:
        sig_html = '<div class="empty">No strong signals right now. Scanner is watching...</div>'

    # Build top 5 metrics
    top5 = ''
    for _,name,_ in COINS[:5]:
        c = coins.get(name)
        if not c: continue
        p = c['price']
        pf = f"${p:,.0f}" if p>100 else f"${p:,.2f}"
        pct_cls = 'pos' if c['pct']>=0 else 'neg'
        top5 += f'''<div class="metric">
            <div class="metric-label">{name}</div>
            <div class="metric-value">{pf}</div>
            <div class="metric-change {pct_cls}">{c['pct']:+.2f}%</div>
        </div>'''

    return f'''<!DOCTYPE html><html><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="30">
<title>X-SHOT Live</title>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;600;700;800&family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:'Outfit',sans-serif;background:#06080f;color:#e8ecf4;min-height:100vh;padding:12px}}
.wrap{{max-width:900px;margin:0 auto}}
.hdr{{display:flex;align-items:center;justify-content:space-between;padding:14px 18px;background:#0c1120;border:1px solid #1e2d4a;border-radius:14px;margin-bottom:12px}}
.hdr h1{{font:800 18px 'JetBrains Mono';color:#ffd600;letter-spacing:3px}}
.hdr small{{font:400 10px 'Outfit';color:#5a6d94}}
.live{{display:flex;align-items:center;gap:6px;font:600 10px 'Outfit';color:#22c55e}}
.dot{{width:7px;height:7px;border-radius:50%;background:#22c55e;animation:blink 1.5s infinite}}
@keyframes blink{{0%,100%{{opacity:1}}50%{{opacity:.2}}}}
.status{{font:400 11px 'JetBrains Mono';color:#5a6d94;text-align:center;margin-bottom:12px}}
.metrics{{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin-bottom:12px}}
.metric{{background:#111827;border:1px solid #1e2d4a;border-radius:10px;padding:12px;text-align:center}}
.metric-label{{font:600 10px 'JetBrains Mono';color:#5a6d94;margin-bottom:4px}}
.metric-value{{font:700 18px 'JetBrains Mono'}}
.metric-change{{font:600 11px 'JetBrains Mono';margin-top:2px}}
.card{{background:#0c1120;border:1px solid #1e2d4a;border-radius:12px;padding:16px;margin-bottom:12px}}
.card-title{{font:700 12px 'JetBrains Mono';color:#ffd600;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:12px}}
table{{width:100%;border-collapse:collapse}}
th{{font:600 9px 'JetBrains Mono';color:#364766;text-transform:uppercase;letter-spacing:1.5px;text-align:left;padding:8px;border-bottom:1px solid #1e2d4a}}
td{{padding:10px 8px;border-bottom:1px solid rgba(30,45,74,.3);font-size:12px}}
tr:hover{{background:#111827}}
.tk{{font:700 13px 'JetBrains Mono';color:#ffd600}}
.nm{{color:#5a6d94;font-size:11px}}
.mono{{font-family:'JetBrains Mono',monospace}}
.sm{{font-size:10px;color:#5a6d94}}
.pos{{color:#22c55e}}.neg{{color:#ff1744}}
.sig-badge{{font:700 9px 'Outfit';padding:3px 10px;border-radius:6px;text-transform:uppercase;letter-spacing:1px}}
.sig-badge.buy{{background:rgba(34,197,94,.12);color:#22c55e}}
.sig-badge.sell{{background:rgba(255,23,68,.12);color:#ff1744}}
.sig-badge.watch{{background:rgba(255,214,0,.1);color:#ffd600}}
.sig-card{{background:#111827;border:1px solid #1e2d4a;border-radius:10px;padding:14px;margin-bottom:8px}}
.sig-top{{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px}}
.sig-bar{{font:500 10px 'JetBrains Mono';color:#364766;margin-bottom:4px}}
.sig-reasons{{font:400 11px 'Outfit';color:#98a8c8;margin-bottom:4px}}
.sig-levels{{font:400 11px 'JetBrains Mono';color:#5a6d94}}
.empty{{text-align:center;padding:30px;color:#364766}}
.foot{{text-align:center;padding:20px;font:400 10px 'Outfit';color:#364766}}
@media(max-width:640px){{.metrics{{grid-template-columns:repeat(2,1fr)}}.hdr{{flex-direction:column;gap:8px;text-align:center}}
th:nth-child(5),td:nth-child(5),th:nth-child(6),td:nth-child(6){{display:none}}}}
</style></head><body>
<div class="wrap">
<div class="hdr">
<div><h1>X-SHOT</h1><small>Halal Crypto Intelligence · Live Dashboard</small></div>
<div class="live"><span class="dot"></span>LIVE · {now_str}</div>
</div>
<div class="status">Last scan: {s['last_scan']} · Scans: {s['scan_count']} · Alerts sent: {s['total_alerts']} · Auto-refresh: 30s</div>
<div class="metrics">{top5}</div>
<div class="card"><div class="card-title">⚡ Active Signals</div>{sig_html}</div>
<div class="card"><div class="card-title">📊 Halal Watchlist</div>
<table><tr><th>Coin</th><th>Name</th><th>Price</th><th>24h</th><th>RSI</th><th>Volume</th><th>Signal</th></tr>{coin_rows}</table>
</div>
<div class="foot">X-SHOT · Scanning every 3 min · ✅ Halal · ⚠️ Not financial advice · Page auto-refreshes every 30s</div>
</div></body></html>'''

def build_api_json():
    with data_lock:
        return json.dumps(STATE, default=str)

class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/api':
            self.send_response(200)
            self.send_header('Content-Type','application/json')
            self.send_header('Access-Control-Allow-Origin','*')
            self.end_headers()
            self.wfile.write(build_api_json().encode())
        else:
            self.send_response(200)
            self.send_header('Content-Type','text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(build_dashboard().encode())
    def log_message(self, *a): pass

# ==================== START ====================
if __name__=='__main__':
    print("="*45)
    print("  X-SHOT — Scanner + Dashboard")
    print(f"  Telegram: {'✅' if TG_TOKEN else '❌ Set TG_TOKEN'}")
    print(f"  Dashboard: http://0.0.0.0:{PORT}")
    print("="*45)

    # Start web server
    server = HTTPServer(('0.0.0.0', PORT), DashboardHandler)
    Thread(target=server.serve_forever, daemon=True).start()
    print(f"  Dashboard server started on port {PORT}")

    # Startup telegram
    if TG_TOKEN:
        tg("🚀 X-SHOT started!\nDashboard + Scanner running 24/7.\nScanning 15 halal coins every 3 min.")

    # Run scanner forever
    scanner_loop()
