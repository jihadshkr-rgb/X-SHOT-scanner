"""
X-SHOT 24/7 Scanner — runs on Render.com / Railway / PythonAnywhere (FREE)
Scans Binance every 3 min, sends Telegram alerts for strong signals only.

DEPLOY IN 2 MINUTES:
1. Go to render.com → New → Background Worker
2. Environment: Python 3
3. Build command: pip install requests
4. Start command: python xshot_scanner.py
5. Add env vars:
   TG_TOKEN=your_bot_token
   TG_CHAT_ID=your_chat_id
6. Deploy → runs 24/7 forever, even when your laptop is OFF
"""
import requests, time, os, json
from datetime import datetime, timezone, timedelta

TG_TOKEN = os.environ.get('TG_TOKEN', '')
TG_CHAT = os.environ.get('TG_CHAT_ID', '')
SCAN_INTERVAL = 180  # 3 minutes

COINS = [
    ('BTCUSDT','BTC'), ('ETHUSDT','ETH'), ('SOLUSDT','SOL'), ('XRPUSDT','XRP'),
    ('ADAUSDT','ADA'), ('AVAXUSDT','AVAX'), ('LINKUSDT','LINK'), ('DOTUSDT','DOT'),
    ('MATICUSDT','MATIC'), ('ATOMUSDT','ATOM'), ('ALGOUSDT','ALGO'), ('FILUSDT','FIL'),
    ('HBARUSDT','HBAR'), ('VETUSDT','VET'), ('NEARUSDT','NEAR'),
]

sent = {}  # {sym: {type, time}} cooldown tracker
COOLDOWN = 1800  # 30 min

def tg(msg):
    if not TG_TOKEN or not TG_CHAT: return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id":TG_CHAT,"text":msg}, timeout=10)
    except Exception as e: print(f"  TG err: {e}")

def candles(sym, interval='1h', limit=60):
    try:
        r = requests.get(f"https://api.binance.com/api/v3/klines",
            params={"symbol":sym,"interval":interval,"limit":limit}, timeout=10)
        return r.json()
    except: return []

def ticker(sym):
    try:
        r = requests.get(f"https://api.binance.com/api/v3/ticker/24hr",
            params={"symbol":sym}, timeout=10)
        return r.json()
    except: return {}

def ema(c, p):
    r=[c[0]]; m=2/(p+1)
    for i in range(1,len(c)): r.append((c[i]-r[-1])*m+r[-1])
    return r

def sma(c, p):
    r=[]
    for i in range(len(c)):
        if i<p-1: r.append(None)
        else: r.append(sum(c[i-p+1:i+1])/p)
    return r

def rsi(c, p=14):
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

def macd_hist(c):
    e12,e26=ema(c,12),ema(c,26)
    ml=[e12[i]-e26[i] for i in range(len(c))]
    sl=ema(ml,9)
    return [ml[i]-sl[i] for i in range(len(ml))]

def bollinger(c, p=20):
    if len(c)<p: return None,None,None
    s=sum(c[-p:])/p
    v=sum((x-s)**2 for x in c[-p:])/p
    sd=v**0.5
    return s+2*sd, s, s-2*sd

def stoch(klines, p=14):
    if len(klines)<p: return 50
    highs=[float(k[2]) for k in klines[-p:]]
    lows=[float(k[3]) for k in klines[-p:]]
    close=float(klines[-1][4])
    hh,ll=max(highs),min(lows)
    return ((close-ll)/(hh-ll)*100) if hh!=ll else 50

def atr(klines, p=14):
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
        if body/rng<0.1: pass  # doji
        if lb>body*2 and ub<body*0.5 and c>o: bull+=1
        if ub>body*2 and lb<body*0.5 and o>c: bear+=1
        if i>0:
            po,pc2=float(klines[i-1][1]),float(klines[i-1][4])
            pb=abs(pc2-po)
            if pc2<po and c>o and body>pb: bull+=1
            if pc2>po and c<o and body>pb: bear+=1
    return bull,bear

def scan():
    jeddah=datetime.now(timezone(timedelta(hours=3)))
    print(f"\n{'='*45}")
    print(f"  X-SHOT Scanner — {jeddah.strftime('%H:%M:%S')} AST")
    print(f"{'='*45}")

    alerts=[]
    for sym,name in COINS:
        try:
            kl=candles(sym)
            if len(kl)<30: continue
            tk=ticker(sym)
            closes=[float(k[4]) for k in kl]
            price=closes[-1]
            pct=float(tk.get('priceChangePercent',0))

            r=rsi(closes)
            h=macd_hist(closes)
            bbu,bbm,bbl=bollinger(closes)
            st=stoch(kl)
            at=atr(kl)
            e20=ema(closes,20)[-1]
            s50=[x for x in sma(closes,50) if x is not None]
            s50v=s50[-1] if s50 else price
            bp,brp=detect_pats(kl)

            bull,bear=0,0
            reasons=[]

            # RSI
            if r<25: bull+=3; reasons.append(f"RSI {r:.0f} deep OS")
            elif r<35: bull+=2; reasons.append(f"RSI {r:.0f} OS")
            elif r>75: bear+=3; reasons.append(f"RSI {r:.0f} deep OB")
            elif r>65: bear+=2; reasons.append(f"RSI {r:.0f} OB")

            # MACD histogram cross (confirmed = 2 bars same direction)
            if len(h)>=3:
                if h[-1]>0 and h[-2]<=0 and h[-3]<=0: bull+=2; reasons.append("MACD bull cross")
                elif h[-1]<0 and h[-2]>=0 and h[-3]>=0: bear+=2; reasons.append("MACD bear cross")
                if h[-1]>0 and h[-1]>h[-2]: bull+=1; reasons.append("MACD rising")
                elif h[-1]<0 and h[-1]<h[-2]: bear+=1; reasons.append("MACD falling")

            # Price vs EMA/SMA stack
            if price>e20 and e20>s50v: bull+=2; reasons.append("Bullish stack")
            elif price<e20 and e20<s50v: bear+=2; reasons.append("Bearish stack")
            elif price>e20: bull+=1
            else: bear+=1

            # Bollinger
            if bbl and price<=bbl*1.002: bull+=2; reasons.append("Lower BB touch")
            elif bbu and price>=bbu*0.998: bear+=2; reasons.append("Upper BB touch")

            # Stochastic
            if st<15: bull+=2; reasons.append(f"Stoch {st:.0f}")
            elif st<25: bull+=1
            elif st>85: bear+=2; reasons.append(f"Stoch {st:.0f}")
            elif st>75: bear+=1

            # Candle patterns
            if bp>=2: bull+=2; reasons.append(f"{bp} bull patterns")
            elif bp==1: bull+=1
            if brp>=2: bear+=2; reasons.append(f"{brp} bear patterns")
            elif brp==1: bear+=1

            net=bull-bear
            sig_type=None
            if net>=5: sig_type='BUY'
            elif net<=-5: sig_type='SELL'

            if sig_type:
                tgt=price+at*3 if sig_type=='BUY' else price-at*3
                stp=price-at*1.5 if sig_type=='BUY' else price+at*1.5
                rr=abs(tgt-price)/abs(stp-price) if abs(stp-price)>0 else 0

                # Cooldown check
                now=time.time()
                prev=sent.get(name)
                if prev and prev['type']==sig_type and (now-prev['time'])<COOLDOWN:
                    print(f"  ⏸️  {name} {sig_type} (cooldown)")
                    continue

                emoji='🟢' if sig_type=='BUY' else '🔴'
                print(f"  {emoji} {sig_type} {name} ${price:,.2f} net={net} ({', '.join(reasons[:3])})")
                alerts.append({
                    'name':name,'type':sig_type,'price':price,'pct':pct,
                    'reasons':reasons[:4],'tgt':tgt,'stp':stp,'rr':rr,'net':net
                })
                sent[name]={'type':sig_type,'time':now}
            else:
                print(f"  {'📈' if net>0 else '📉' if net<0 else '➡️'}  {name} ${price:,.2f} net={net}")

            time.sleep(0.15)
        except Exception as e:
            print(f"  ❌ {name}: {e}")

    if alerts:
        msg=f"⚡ X-SHOT Alert\n{jeddah.strftime('%H:%M')} AST\n\n"
        for a in alerts:
            e='🟢' if a['type']=='BUY' else '🔴'
            msg+=f"{e} {a['type']} {a['name']} ${a['price']:,.2f} ({a['pct']:+.1f}%)\n"
            msg+=f"  {' · '.join(a['reasons'])}\n"
            msg+=f"  Target ${a['tgt']:,.2f} | Stop ${a['stp']:,.2f} | R:R {a['rr']:.1f}\n"
            msg+=f"  Strength: {abs(a['net'])}/12\n\n"
        msg+="✅ Halal · Not financial advice"
        tg(msg)
        print(f"\n  📨 Sent {len(alerts)} alerts to Telegram")
    else:
        print(f"\n  😴 No strong signals")

if __name__=='__main__':
    print("="*45)
    print("  X-SHOT 24/7 SCANNER")
    print(f"  Telegram: {'✅' if TG_TOKEN else '❌ Set TG_TOKEN env var'}")
    print(f"  Interval: {SCAN_INTERVAL}s")
    print("="*45)
    if not TG_TOKEN:
        print("\n⚠️  export TG_TOKEN='your_bot_token'")
        print("   export TG_CHAT_ID='your_chat_id'")
    while True:
        try: scan()
        except Exception as e: print(f"Error: {e}")
        print(f"  ⏳ Next scan in {SCAN_INTERVAL//60} min...")
        time.sleep(SCAN_INTERVAL)
