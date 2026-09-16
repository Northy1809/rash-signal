---
type: project
created: 2026-09-09
updated: 2026-09-15
tags: [rash-signal, polymarket, cs2, esports-trading, backtest, monte-carlo, limits]
status: brief - fase 4 paper-mode kører, WSS + half-Kelly aktiveret 2026-09-15
---

# rash-signal — data-drevet CS2-strategi på Polymarket (100 % gratis)

**Hvad det er:** en systematisk CS2-handelsstrategi på Polymarket, fundet ved
at backteste hundredvis af simple regler mod historisk kursdata + resultater.
Rash-Barometer er beviset at edge findes i denne kategori — han er vores
sanity-check, ikke vores target.

**Hvor koden ligger:** `projects/rash-signal/` (endnu tom, byggeklar).
**Data:** `scratchpad/rash-barometer/`

**Baggrund:** [[../../memory/decisions/2026-09-09-rash-barometer-cs2-bot-analyse]]
og live-rapport: <https://claude.ai/code/artifact/e25b29f9-93af-4772-a145-abaff1e384bf>

**Status:** fase 0 kører (bulk-scrape af Polymarket prices-history i baggrunden).
**Budget:** 0 kr — alt data er gratis og offentligt.

---

## 0. HARD LIMITS — den ufravigelige regel botten skal bygges efter

**Botten må ALDRIG placere en order over max_limit_price.** Alle ordrer er
LIMIT orders, aldrig market. Fill dropper vi hellere end vi handler over cap.

### Break-even formel (verificeret)

For $100 stake ved entry `e` med winrate `w`:
```
e_max = [(w+20) - √((w+20)² - 80w²)] / (2w)
```

Polymarket sports fee: `fee_shares = shares × 0.05 × p × (1-p)`.

### Sikkerhedsantagelser (må ikke ændres uden ny backtest)

- **Winrate stress:** antag observeret winrate falder 5 procentpoint.
- **Ekstra safety:** limit-pris = break-even × 0.95 (5 % pris-buffer).
- **Slippage:** antag +0.01 slippage oveni det observerede best-ask.

### HARD LIMITS Strategy A — mirror Rash's big trades

Kun mirror hvis Rash's entry-pris ligger i én af disse to buckets, ellers **skip**:

| Bucket | Empirisk WR (30d) | Max limit pris | Half-Kelly stake | Position |
|---|---|---|---|---|
| 0.55 – 0.65 | **69,2 %** (n=39) | **0.622** | **9,3 %** af bankroll | BUY hvis eff_ask ≤ 0.622 |
| 0.65 – 0.75 | **78,3 %** (n=46) | **0.716** | **11,8 %** af bankroll | BUY hvis eff_ask ≤ 0.716 |

Buckets 0.30, 0.40, 0.50, 0.80, 0.90 er **eksplicit forbudte** — worst-case
winrate gør dem tabende. Bucket 0.20 og lavere er også forbudte.

**Bucket 0.35-0.45 (0.4) blev fjernet 2026-09-15** efter frisk 30d-verifikation:
empirisk WR 41,0 % (n=61) < brief's antaget worst-case 46 %. Half-Kelly = -1,8 %
(negativ Kelly ⇒ EV-negativ over tid). Se `memory/lessons/2026-09-15-rash-edge-verificeret-30d.md`.

**Signal-kilde:** `wss://ws-live-data.polymarket.com` topic `activity/orders_matched`
med client-side filter på `proxyWallet == 0x29b52d98ac9ef9414b04164246c95bc63d74cc6c`.
Målt Polymarket indekser-latency 865-910 ms + netværk ~200 ms = **~1,0-1,2 s
end-to-end** (vs. tidligere REST-polling ~9-15 s). REST bevares som catch-up ved
WSS-reconnect og som fallback i `--tick`-mode (GH Actions cron).

**Sizing:** Half-Kelly kalibreret på 30d empirisk WR (2026-09-15). Fixed $100
stake er suboptimalt — Kelly captures ~9-12× større stakes hvor edge er
stærkest. Half-Kelly (½·f*) er professionel standard: 75 % af growth-rate
ved 25 % varians vs. full Kelly. Cap $2000/trade som safety-net.

### HARD LIMITS Strategy D — crash-buy > 20 % i 30 min

Kun buy hvis current price ligger i én af disse fem buckets:

| Bucket | n | Avg lift 60 min | Net PnL per $100 |
|---|---|---|---|
| 0.10 – 0.20 | 7 | +22 % | +$10,75 |
| 0.20 – 0.30 | 53 | +25 % | +$19,50 |
| 0.30 – 0.40 | 132 | +36 % | +$28,61 |
| 0.40 – 0.50 | 172 | +21 % | +$15,48 |
| 0.50 – 0.60 | 154 | +13 % | +$8,93 |

Max limit = current price + 0.005 (lille buffer for spread).
Exit = +60 min efter fill, uanset pris.
Bucket 0.70+ er forbudt (kun 5 observationer, avg –5 % lift).

### Consequences

- Filteret dropper 64 % af Rash's mirror-signaler, bevarer 91 % af hans EV.
- Ved edge-decay op til 5 pct-points forbliver netto-EV positiv.
- Hvis winrate falder mere: kør backtest igen og genberegn limits, aldrig
  loose'n regler i live-drift.

### Verifikationskrav før live-deploy

1. Genberegn winrate per bucket på seneste 30 dage af Rash's trades → limits
   bekræftes eller genberegnes.
2. Alle limits testet i paper-mode i mindst 7 dage.
3. Bot LOGGER hver skip med begrundelse (bucket/limit-overskredet) så vi kan
   audite at reglen håndhæves.

Se hele matematik-rapporten: `scratchpad/rash-barometer/phase4_limits.py`.

---

## 1. Hvorfor ikke bare fitte til Rash?

To fælder:

**Overfitting.** Hvis vi tester 10.000 strategier og finder én der matcher
95 % af Rash's handler, er den næsten sikkert overfittet til hans historik.
Perfekt fit på fortiden giver os ingen edge fremad.

**Manglende non-trades.** Vi ser HANS handler. Vi ser IKKE hvornår han
overvejede og afholdt sig. Vi kan ikke rekonstruere hans beslutnings-funktion
fra kun én side af den — kun hans handlings-mønster.

Så: Rash beviser at edge findes. Vores job er at finde vores egen edge på
samme data, med hans handler som sanity-check.

## 2. Datakilder (alle gratis)

- **Polymarket CLOB `/prices-history`** — hvert pris-tick på hver CS2-market.
  1-min fidelity med tight startTs/endTs. Verificeret virker 9/9.
- **HLTV.org** — round-by-round match-data + pre-match bookmaker-odds.
  Uofficiel API (`python-hltv`, `hltv-api.vercel.app`). Gratis, rate-limited.
- **Rash's 10.500 trades** — vi har dem allerede i
  `scratchpad/rash-barometer/trades_clean.json`
- **Polymarket resolutions** — via `data-api.polymarket.com/positions`, gratis

## 3. Fase 0 — bulk-scrape (kører nu)

Henter `/prices-history` for alle 1841 unique outcome-tokens Rash har
handlet. 1-min fidelity, vindue = første trade – 2h → sidste trade + 2h.
Gemmer som `scratchpad/rash-barometer/prices/<asset>.json`.
Pace: 4 req/sec. ETA: ~8 min. Total data: ~50-100 MB.

## 4. Fase 1 — kortlægge Rash's regler (2 dage)

For hver af hans 10.500 trades: hvad var markedstilstanden umiddelbart før?
- Polymarket-pris på outcome (fra vores scrape)
- Modstander-outcome-pris (implied YES+NO sum kan afsløre arb-vindue)
- Tid til match-start (fra event-slug)
- Volatility de sidste 30/60/300 sekunder
- Om der har været en "burst" af trades fra andre wallets

Cluster hans trades → udled 3-5 sandsynlige regel-typer. Dette er
hypotese-generatoren, ikke deployment-strategien.

## 5. Fase 2 — backtest 100+ simple regler (2 dage)

**Kandidat-regler** (start-liste, ikke udtømmende):

A. Buy favorit under 0.65 og hold til resolution
B. Buy underdog over 0.35 hvis pris er faldet >10 % i sidste 30 min (mean-reversion)
C. Buy hvis YES+NO summer til <0.98 (billig arb-mulighed)
D. Buy hvis pris har ligget stille (<±2 %) i 20+ min efter volatility (return to mean)
E. Buy underdog efter et pris-crash på >20 % (overreaction fade)
F. Buy favorit i det sidste vindue før match-start (last-minute sharp money bias)
G. Buy hvis Rash lige har handlet (mirror-copy som benchmark)
H. Kombination af flere signaler med threshold-vote

**Split:** jan-jun 2026 = train, jul-sep = test.
**Overfit-guard:**
- Krav: positiv PnL på BEGGE sæt, ikke bare train
- Minimum 100 trades per regel i test-sæt
- Sharpe > 1 og max DD < 40 % af aktiveret kapital
- Wiggle-test: performance falder ikke katastrofalt når parametre ændres ±20 %

## 6. Fase 3 — monte carlo på parametre (1-2 dage)

For top-3 regler fra fase 2, variér parametrene fint-gearet:
- Buy-threshold (fx 0.60, 0.62, 0.64...)
- Holding period (til slut vs. exit ved +X %)
- Sizing (fixed vs. Kelly-fraction)

Rank parametervalg efter robusthed, ikke bare peak-performance.
Beware: 1000 parameter-combos × 10.000 trades = mange spurious peaks.
Bruger multiple-testing-correction eller kun deploy configs der er top-quartile
i BÅDE Sharpe og drawdown.

## 7. Fase 4 — paper-trade (30 dage)

Deploy top-strategi i papir-mode: script beregner ordrer i real-time, logger dem,
placerer ingen. Cross-tjek mod Rash's live-handler: matcher vores signal hans?
Er timing-forskellen forudsigelig?

Krav for at gå live: paper-PnL positiv i 30 dage, minimum 100 signaler,
Sharpe > 1.5.

## 8. Fase 5 — live (uge 6+)

Start med $500-1.000 kapital. Skalér kun ved bekræftet edge på live-trades:
minimum 30 dages positiv netto-PnL efter fees, max DD < 30 %.

## 9. Kill-signaler

- **Fase 1:** ingen af hans burst-mønstre klynger meningsfuldt → ren støj
- **Fase 2:** ingen regel har positiv PnL på både train og test → intet edge
  findes i simpel-regel-rummet
- **Fase 4:** paper < 0 efter 30 dage → deploy stoppet
- **Fase 5:** live < 0 efter 30 dage → strategi død, arkivér indsigt

## 10. Hvorfor det er bedre end at prøve at kopiere Rash

- **Gratis:** ingen abonnementer, ingen API-nøgler
- **Ikke overfittet:** train/test-split fanger overfit
- **Uafhængig:** hvis Rash's edge dør, kan vores overleve
- **Skalerbart:** samme metode virker på LoL, Dota, sport, valgmarkeder
- **Læringsværdi:** vi lærer domænet, ikke bare hans mønster
- **Direkte kobling til trade-journal:** botten bliver testkunde #2 for
  produktet og genererer præcis den slags handelslog vi skal analysere

## 11. Åbne spørgsmål (ikke-blokerende)

- Skal HLTV-scraping køres allerede i fase 1 eller først i fase 2? →
  Fase 2, kun for de matches vores top-regler triggede på
- Egen Polygon RPC-node eller Alchemy? → Alchemy free tier indtil live
- Hvor tidligt tages Rash's live-handler ind som feature? → Fase 4 som
  sanity-check, ikke som input til beslutning

## 12. Første konkrete handling (kører nu)

Fase 0-scrape kører i baggrunden. Log: `scratchpad/rash-barometer/scrape.log`.
Når færdig (~8 min): jeg raporterer coverage-tal og starter fase 1.
